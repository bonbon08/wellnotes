// Two-way sync with Nextcloud/WebDAV (port of wellnotes/sync.py).
//
// Same rules and the same state file (.wellnotes-sync.json) as the Linux app:
// changed on one side -> copy over; changed on both -> local wins, the other version is
// kept as "(Konflikt …)" copy; deleted on one side and unchanged on the other -> delete
// (local files go to <library>/.trash).
import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter/foundation.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:http/http.dart' as http;
import 'package:path/path.dart' as p;
import 'package:shared_preferences/shared_preferences.dart';
import 'package:xml/xml.dart';

const _ext = '.goodnotes';
const stateFile = '.wellnotes-sync.json';

class SyncError implements Exception {
  final String message;
  SyncError(this.message);
  @override
  String toString() => message;
}

class SyncConfig {
  bool enabled = false;
  bool auto = true;
  int interval = 5;
  String server = '';
  String user = '';
  String folder = 'WellNotes';
  String password = '';

  static const _secure = FlutterSecureStorage();

  String get key => 'nc:$server:$user:$folder';

  static Future<SyncConfig> load() async {
    final c = SyncConfig();
    final prefs = await SharedPreferences.getInstance();
    c.enabled = prefs.getBool('sync.enabled') ?? false;
    c.auto = prefs.getBool('sync.auto') ?? true;
    c.interval = prefs.getInt('sync.interval') ?? 5;
    c.server = prefs.getString('sync.server') ?? '';
    c.user = prefs.getString('sync.user') ?? '';
    c.folder = prefs.getString('sync.folder') ?? 'WellNotes';
    try {
      c.password = await _secure.read(key: 'nextcloud:${c.user}@${c.server}') ?? '';
    } on Object {
      c.password = '';
    }
    return c;
  }

  Future<void> save() async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.setBool('sync.enabled', enabled);
    await prefs.setBool('sync.auto', auto);
    await prefs.setInt('sync.interval', interval);
    await prefs.setString('sync.server', server);
    await prefs.setString('sync.user', user);
    await prefs.setString('sync.folder', folder);
    await _secure.write(key: 'nextcloud:$user@$server', value: password);
  }
}

class WebDav {
  final String base; // full URL of the sync folder
  final Map<String, String> headers;
  final String basePath;
  final Set<String> _dirs = {};
  final http.Client _client = http.Client();

  WebDav._(this.base, this.headers, this.basePath);

  factory WebDav(SyncConfig c) {
    if (c.server.isEmpty || c.user.isEmpty || c.password.isEmpty) {
      throw SyncError('Nextcloud: Server, Benutzer und App-Passwort angeben');
    }
    var server = c.server.trim().replaceAll(RegExp(r'/+$'), '');
    if (!server.contains('://')) server = 'https://$server';
    final folder = c.folder.replaceAll(RegExp(r'^/+|/+$'), '');
    final root = server.contains('/remote.php/') || Uri.parse(server).path.contains('/dav')
        ? server
        : '$server/remote.php/dav/files/${Uri.encodeComponent(c.user)}';
    final base = '$root/${(folder.isEmpty ? 'WellNotes' : folder).split('/').map(Uri.encodeComponent).join('/')}';
    final auth = base64.encode(utf8.encode('${c.user}:${c.password}'));
    return WebDav._(base, {'Authorization': 'Basic $auth', 'User-Agent': 'WellNotes-Android'},
        Uri.decodeFull(Uri.parse(base).path).replaceAll(RegExp(r'/+$'), ''));
  }

  String url(String rel) => '$base/${rel.split('/').map(Uri.encodeComponent).join('/')}';

  Future<http.StreamedResponse> _send(String method, String url, {List<int>? body, Map<String, String>? extra}) async {
    final req = http.Request(method, Uri.parse(url))..headers.addAll({...headers, ...?extra});
    if (body != null) req.bodyBytes = body;
    try {
      return await _client.send(req).timeout(const Duration(seconds: 120));
    } on SocketException catch (e) {
      throw SyncError('Verbindung fehlgeschlagen: ${e.message}');
    } on TimeoutException {
      throw SyncError('Zeitüberschreitung bei der Verbindung');
    } on http.ClientException catch (e) {
      throw SyncError('Verbindung fehlgeschlagen: ${e.message}');
    }
  }

  void _check(int status, String what) {
    if (status == 401 || status == 403) throw SyncError('Nextcloud: Anmeldung abgelehnt (Benutzer/App-Passwort prüfen)');
    if (status >= 400) throw SyncError('Nextcloud: $what fehlgeschlagen (HTTP $status)');
  }

  static const _propfind = '<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop><d:getetag/>'
      '<d:resourcetype/><d:getlastmodified/></d:prop></d:propfind>';

  Future<(int, String)> _pf(String u, int depth) async {
    final r = await _send('PROPFIND', u,
        body: utf8.encode(_propfind), extra: {'Depth': '$depth', 'Content-Type': 'application/xml'});
    return (r.statusCode, await r.stream.bytesToString());
  }

  Future<void> _mkdirs(String relDir) async {
    final origin = Uri.parse(base).origin;
    final full = relDir.isEmpty ? basePath : '$basePath/$relDir';
    const marker = '/remote.php/dav/files/';
    String path;
    List<String> segs;
    if (full.contains(marker)) {
      final i = full.indexOf(marker);
      final tail = full.substring(i + marker.length);
      final slash = tail.indexOf('/');
      path = full.substring(0, i) + marker + (slash < 0 ? tail : tail.substring(0, slash));
      segs = slash < 0 ? [] : tail.substring(slash + 1).split('/');
    } else {
      path = '';
      segs = full.split('/');
    }
    for (final seg in segs.where((s) => s.isNotEmpty)) {
      path += '/$seg';
      if (_dirs.contains(path)) continue;
      final r = await _send('MKCOL', origin + path.split('/').map(Uri.encodeComponent).join('/'));
      await r.stream.drain<void>();
      final inside = path.length >= basePath.length;
      if (![201, 405, 301, 302].contains(r.statusCode) && inside) _check(r.statusCode, 'Ordner anlegen');
      _dirs.add(path);
    }
  }

  Future<Map<String, String>> list() async {
    final (st0, _) = await _pf(base, 0);
    if (st0 == 404) {
      await _mkdirs('');
    } else {
      _check(st0, 'Verbinden');
    }
    final out = <String, String>{};
    final todo = [base];
    final seen = <String>{};
    while (todo.isNotEmpty) {
      final u = todo.removeLast();
      if (!seen.add(u)) continue;
      final (st, body) = await _pf(u, 1);
      _check(st, 'Auflisten');
      final xml = XmlDocument.parse(body);
      for (final resp in xml.findAllElements('response', namespaceUri: 'DAV:')) {
        final href = resp.getElement('href', namespaceUri: 'DAV:')?.innerText ?? '';
        final path = Uri.decodeFull(Uri.parse(href).path).replaceAll(RegExp(r'/+$'), '');
        if (!path.startsWith(basePath)) continue;
        final rel = path.substring(basePath.length).replaceAll(RegExp(r'^/+'), '');
        if (rel.isEmpty || rel.split('/').any((s) => s.startsWith('.'))) continue;
        final isDir = resp.findAllElements('collection', namespaceUri: 'DAV:').isNotEmpty;
        if (isDir) {
          _dirs.add(path);
          todo.add(url(rel));
        } else if (rel.toLowerCase().endsWith(_ext)) {
          final etag = resp.findAllElements('getetag', namespaceUri: 'DAV:').firstOrNull?.innerText ?? '';
          final mod = resp.findAllElements('getlastmodified', namespaceUri: 'DAV:').firstOrNull?.innerText ?? '';
          out[rel] = etag.isNotEmpty ? etag : mod;
        }
      }
    }
    return out;
  }

  Future<void> download(String rel, String dest) async {
    final r = await _send('GET', url(rel));
    _check(r.statusCode, 'Herunterladen');
    final sink = File(dest).openWrite();
    await r.stream.pipe(sink);
  }

  Future<String> upload(String rel, String src) async {
    if (rel.contains('/')) await _mkdirs(rel.substring(0, rel.lastIndexOf('/')));
    final r = await _send('PUT', url(rel), body: await File(src).readAsBytes(), extra: {'Content-Type': 'application/zip'});
    await r.stream.drain<void>();
    _check(r.statusCode, 'Hochladen');
    var etag = r.headers['oc-etag'] ?? r.headers['etag'];
    if (etag == null) {
      final (st, body) = await _pf(url(rel), 0);
      if (st < 400) etag = XmlDocument.parse(body).findAllElements('getetag', namespaceUri: 'DAV:').firstOrNull?.innerText;
    }
    return etag ?? '${DateTime.now().millisecondsSinceEpoch}';
  }

  Future<void> delete(String rel) async {
    final r = await _send('DELETE', url(rel));
    await r.stream.drain<void>();
    if (r.statusCode != 404) _check(r.statusCode, 'Löschen');
  }

  void close() => _client.close();
}

List<String> scanFiles(String root) {
  final out = <String>[];
  final dir = Directory(root);
  if (!dir.existsSync()) return out;
  for (final e in dir.listSync(recursive: true, followLinks: false)) {
    final rel = p.relative(e.path, from: root);
    if (e is! File || !rel.toLowerCase().endsWith(_ext)) continue;
    if (p.split(rel).any((s) => s.startsWith('.'))) continue;
    out.add(p.split(rel).join('/'));
  }
  return out;
}

String _localVersion(String path) {
  final st = File(path).statSync();
  return '${st.modified.microsecondsSinceEpoch * 1000}-${st.size}';
}

String _conflictName(String rel) {
  final ext = p.extension(rel);
  final base = rel.substring(0, rel.length - ext.length);
  final n = DateTime.now();
  String two(int v) => v.toString().padLeft(2, '0');
  return '$base (Konflikt ${n.year}-${two(n.month)}-${two(n.day)} ${two(n.hour)}${two(n.minute)})$ext';
}

class SyncResult {
  int uploaded = 0, downloaded = 0, deleted = 0;
  final List<String> conflicts = [], skipped = [];

  String summary() {
    final parts = [
      if (uploaded > 0) '$uploaded hochgeladen',
      if (downloaded > 0) '$downloaded heruntergeladen',
      if (deleted > 0) '$deleted gelöscht',
      if (conflicts.isNotEmpty) '${conflicts.length} Konflikt(e)',
    ];
    return parts.isEmpty ? 'Alles aktuell' : parts.join(', ');
  }
}

Future<SyncResult> runSync(String root, WebDav backend, {Set<String> busy = const {}, String key = ''}) async {
  final statePath = p.join(root, stateFile);
  var files = <String, Map<String, String>>{};
  try {
    final state = jsonDecode(await File(statePath).readAsString()) as Map<String, Object?>;
    if (state['key'] == key) {
      (state['files'] as Map).forEach((k, v) => files[k as String] = (v as Map).cast<String, String>());
    }
  } on Object {
    files = {};
  }
  final remote = await backend.list();
  final local = {for (final rel in scanFiles(root)) rel: _localVersion(p.join(root, rel))};
  final res = SyncResult();
  final tmpDir = await Directory.systemTemp.createTemp('wellnotes-sync-');
  String lpath(String rel) => p.joinAll([root, ...rel.split('/')]);

  Future<String> download(String rel, [String? target]) async {
    final dest = lpath(target ?? rel);
    await Directory(p.dirname(dest)).create(recursive: true);
    final tmp = p.join(tmpDir.path, 'dl');
    await backend.download(rel, tmp);
    await File(tmp).copy(dest);
    res.downloaded++;
    return _localVersion(dest);
  }

  Future<String> upload(String rel) async {
    final v = await backend.upload(rel, lpath(rel));
    res.uploaded++;
    return v;
  }

  Future<void> trash(String rel) async {
    final dest = p.join(root, '.trash', '${DateTime.now().millisecondsSinceEpoch}-${p.basename(rel)}');
    await Directory(p.dirname(dest)).create(recursive: true);
    await File(lpath(rel)).rename(dest);
  }

  try {
    final all = {...local.keys, ...remote.keys, ...files.keys}.toList()..sort();
    for (final rel in all) {
      final L = local[rel], R = remote[rel], st = files[rel];
      final isBusy = busy.contains(lpath(rel));
      if (L != null && R != null) {
        if (st == null) {
          final tmp = p.join(tmpDir.path, 'cmp');
          await backend.download(rel, tmp);
          if (listEquals(await File(tmp).readAsBytes(), await File(lpath(rel)).readAsBytes())) {
            files[rel] = {'l': L, 'r': R};
            continue;
          }
          final crel = _conflictName(rel);
          await File(tmp).copy(lpath(crel));
          files[crel] = {'l': _localVersion(lpath(crel)), 'r': await backend.upload(crel, lpath(crel))};
          files[rel] = {'l': L, 'r': await upload(rel)};
          res.conflicts.add(rel);
          continue;
        }
        final lc = L != st['l'], rc = R != st['r'];
        if (lc && !rc) {
          files[rel] = {'l': L, 'r': await upload(rel)};
        } else if (rc && !lc) {
          if (isBusy) {
            res.skipped.add(rel);
            continue;
          }
          files[rel] = {'l': await download(rel), 'r': R};
        } else if (lc && rc) {
          final crel = _conflictName(rel);
          final lv = await download(rel, crel);
          files[crel] = {'l': lv, 'r': await backend.upload(crel, lpath(crel))};
          files[rel] = {'l': L, 'r': await upload(rel)};
          res.conflicts.add(rel);
        }
      } else if (L != null) {
        if (st != null && L == st['l']) {
          if (isBusy) {
            res.skipped.add(rel);
            continue;
          }
          await trash(rel);
          files.remove(rel);
          res.deleted++;
        } else {
          files[rel] = {'l': L, 'r': await upload(rel)};
        }
      } else if (R != null) {
        if (st != null && R == st['r']) {
          await backend.delete(rel);
          files.remove(rel);
          res.deleted++;
        } else {
          files[rel] = {'l': await download(rel), 'r': R};
        }
      } else {
        files.remove(rel);
      }
    }
    return res;
  } finally {
    await File(statePath)
        .writeAsString(jsonEncode({'key': key, 'files': files, 'last': DateTime.now().millisecondsSinceEpoch / 1000}));
    await tmpDir.delete(recursive: true);
  }
}

/// Runs syncs in the background and exposes their state to the UI.
class SyncManager extends ChangeNotifier {
  final String Function() root;
  final Set<String> Function() busy;
  SyncConfig config = SyncConfig();
  bool running = false;
  String? lastMessage;
  bool lastError = false;
  DateTime? last;
  Timer? _timer;
  bool _pending = false;

  SyncManager(this.root, this.busy);

  Future<void> init() async {
    config = await SyncConfig.load();
    reschedule();
    if (config.enabled && config.auto) unawaited(sync(quiet: true));
  }

  void reschedule() {
    _timer?.cancel();
    if (config.enabled && config.auto) {
      _timer = Timer.periodic(Duration(minutes: config.interval.clamp(1, 120)), (_) => sync(quiet: true));
    }
  }

  Future<void> sync({bool quiet = false}) async {
    if (!config.enabled) return;
    if (running) {
      _pending = true;
      return;
    }
    running = true;
    notifyListeners();
    WebDav? dav;
    try {
      dav = WebDav(config);
      final r = await runSync(root(), dav, busy: busy(), key: config.key);
      final msg = r.summary() + (r.conflicts.isNotEmpty ? ' – Konfliktkopien angelegt' : '');
      lastMessage = quiet && msg == 'Alles aktuell' ? null : msg;
      lastError = false;
    } on SyncError catch (e) {
      final show = !quiet || e.message != lastMessage;
      lastMessage = show ? e.message : null;
      lastError = true;
    } on Object catch (e) {
      lastMessage = 'Synchronisation fehlgeschlagen: $e';
      lastError = true;
    } finally {
      dav?.close();
      running = false;
      last = DateTime.now();
      notifyListeners();
    }
    if (_pending) {
      _pending = false;
      await sync(quiet: true);
    }
  }

  Future<String> test() async {
    final dav = WebDav(config);
    try {
      final n = (await dav.list()).length;
      return 'Verbindung OK – $n Notizbuch/Notizbücher in der Nextcloud';
    } finally {
      dav.close();
    }
  }

  @override
  void dispose() {
    _timer?.cancel();
    super.dispose();
  }
}
