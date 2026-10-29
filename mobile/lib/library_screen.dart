// Library: folders and notebooks (.goodnotes files) with covers, search, import and sync.
import 'dart:io';
import 'dart:typed_data';

import 'package:file_picker/file_picker.dart';
import 'package:flutter/material.dart' hide Element, Page;
import 'package:path/path.dart' as p;
import 'package:share_plus/share_plus.dart';

import 'editor_screen.dart';
import 'importers.dart';
import 'main.dart';
import 'model.dart';
import 'paper.dart';
import 'settings_screen.dart';
import 'storage.dart';

class _Item {
  final bool folder;
  final String path;
  final DateTime modified;
  _Item(this.folder, this.path, this.modified);
  String get name => folder ? p.basename(path) : p.basenameWithoutExtension(path);
}

class LibraryScreen extends StatefulWidget {
  final AppState state;
  const LibraryScreen({super.key, required this.state});

  @override
  State<LibraryScreen> createState() => _LibraryScreenState();
}

class _LibraryScreenState extends State<LibraryScreen> {
  late String folder = widget.state.root;
  String query = '';
  bool searching = false;
  bool sortByName = false;
  final Map<String, (DateTime, Uint8List?, String?)> _covers = {};
  String? _importStatus;
  bool _cancelImport = false;

  AppState get app => widget.state;

  @override
  void initState() {
    super.initState();
    app.sync.addListener(_onSync);
  }

  @override
  void dispose() {
    app.sync.removeListener(_onSync);
    super.dispose();
  }

  void _onSync() {
    if (!mounted) return;
    setState(() {});
    if (!app.sync.running && app.sync.lastMessage != null) {
      _toast(app.sync.lastMessage!);
      app.sync.lastMessage = null;
    }
  }

  void _toast(String msg) => ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(msg)));

  List<_Item> _items() {
    final dir = Directory(folder);
    if (!dir.existsSync()) return [];
    final out = <_Item>[];
    for (final e in dir.listSync()) {
      final name = p.basename(e.path);
      if (name.startsWith('.')) continue;
      if (e is Directory) {
        out.add(_Item(true, e.path, e.statSync().modified));
      } else if (name.toLowerCase().endsWith(ext)) {
        out.add(_Item(false, e.path, e.statSync().modified));
      }
    }
    final q = query.toLowerCase();
    out.retainWhere((i) => q.isEmpty || i.name.toLowerCase().contains(q));
    out.sort((a, b) {
      if (a.folder != b.folder) return a.folder ? -1 : 1;
      return sortByName ? a.name.toLowerCase().compareTo(b.name.toLowerCase()) : b.modified.compareTo(a.modified);
    });
    return out;
  }

  Future<void> _loadCover(_Item it) async {
    final cached = _covers[it.path];
    if (cached != null && cached.$1 == it.modified) return;
    _covers[it.path] = (it.modified, cached?.$2, cached?.$3);
    final (thumb, cover) = await readCover(it.path);
    if (!mounted) return;
    setState(() => _covers[it.path] = (it.modified, thumb, cover));
  }

  Future<void> _open(String path, [Document? doc]) async {
    try {
      final d = doc ?? await loadDocument(path);
      if (!mounted) return;
      app.openPaths.add(path);
      await Navigator.of(context).push(MaterialPageRoute<void>(builder: (_) => EditorScreen(state: app, doc: d)));
    } on Object catch (e) {
      _toast('Öffnen fehlgeschlagen: $e');
    } finally {
      app.openPaths.remove(path);
      if (mounted) setState(() {});
      if (app.sync.config.enabled && app.sync.config.auto) app.sync.sync(quiet: true);
    }
  }

  // ------------------------------------------------------------------ actions

  Future<void> _newNotebook() async {
    final result = await showDialog<(String, String, String, List<double>, String)>(
        context: context, builder: (_) => const NewNotebookDialog());
    if (result == null) return;
    final (title, size, paper, color, cover) = result;
    final doc = newNotebook(title, size: size, paper: paper, color: color, cover: cover);
    final path = uniquePath(folder, title, ext);
    await saveDocument(doc, path);
    setState(() {});
    await _open(path, doc);
  }

  Future<void> _newFolder() async {
    final name = await _askText('Neuer Ordner', 'Neuer Ordner');
    if (name == null || name.isEmpty) return;
    await Directory(uniquePath(folder, name)).create();
    setState(() {});
  }

  Future<String?> _askText(String title, String initial) {
    final ctl = TextEditingController(text: initial);
    return showDialog<String>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: Text(title),
        content: TextField(controller: ctl, autofocus: true, onSubmitted: (v) => Navigator.pop(ctx, v.trim())),
        actions: [
          TextButton(onPressed: () => Navigator.pop(ctx), child: const Text('Abbrechen')),
          FilledButton(onPressed: () => Navigator.pop(ctx, ctl.text.trim()), child: const Text('OK')),
        ],
      ),
    );
  }

  Future<void> _import() async {
    final files = await FilePicker.pickFiles(
        type: FileType.custom, allowedExtensions: [for (final e in importExts) e.substring(1)], dialogTitle: 'Importieren');
    if (files.isEmpty) return;
    final created = <String>[];
    for (final f in files) {
      try {
        // Content URIs have no file path: copy to a temporary file with the original name.
        final tmp = await Directory.systemTemp.createTemp('wellnotes-in-');
        final local = p.join(tmp.path, f.name);
        await File(local).writeAsBytes(await f.readAsBytes());
        if (f.name.toLowerCase().endsWith('.zip')) {
          await _bulk(() => importZip(local, folder, progress: _progress, cancelled: () => _cancelImport), f.name);
        } else {
          setState(() => _importStatus = 'Importiere „${f.name}“ …');
          created.add(await importFile(local, folder));
        }
        await tmp.delete(recursive: true);
      } on Object catch (e) {
        _toast('Import von ${f.name} fehlgeschlagen: $e');
      }
    }
    setState(() => _importStatus = null);
    if (created.length == 1) {
      await _open(created.single);
    } else if (created.isNotEmpty) {
      _toast('${created.length} Dateien importiert');
    }
  }

  Future<void> _importFolder() async {
    final dir = await FilePicker.getDirectoryPath(dialogTitle: 'Obsidian-Vault oder Markdown-Ordner wählen');
    if (dir == null) return;
    if (!Directory(dir).existsSync()) {
      _toast('Auf diesen Ordner kann WellNotes nicht direkt zugreifen. Bitte den Vault als ZIP importieren.');
      return;
    }
    await _bulk(() => importFolder(dir, folder, progress: _progress, cancelled: () => _cancelImport), p.basename(dir));
  }

  void _progress(int done, int total, String current) {
    if (mounted) setState(() => _importStatus = 'Importiere: $done von $total ${p.basename(current)}');
  }

  Future<void> _bulk(Future<(String, int, List<String>)> Function() run, String name) async {
    _cancelImport = false;
    setState(() => _importStatus = 'Importiere „$name“ …');
    try {
      final (_, count, errors) = await run();
      _toast('$count Notizbücher aus „$name“ importiert${errors.isEmpty ? '' : ' (${errors.length} Fehler)'}');
    } on Object catch (e) {
      _toast('Import fehlgeschlagen: $e');
    } finally {
      if (mounted) setState(() => _importStatus = null);
    }
  }

  Future<void> _itemMenu(_Item it) async {
    final action = await showModalBottomSheet<String>(
      context: context,
      showDragHandle: true,
      builder: (ctx) => SafeArea(
        child: Column(mainAxisSize: MainAxisSize.min, children: [
          ListTile(title: Text(it.name, style: Theme.of(ctx).textTheme.titleMedium)),
          ListTile(leading: const Icon(Icons.edit_outlined), title: const Text('Umbenennen'), onTap: () => Navigator.pop(ctx, 'rename')),
          if (!it.folder)
            ListTile(leading: const Icon(Icons.copy_outlined), title: const Text('Duplizieren'), onTap: () => Navigator.pop(ctx, 'dup')),
          if (!it.folder)
            ListTile(leading: const Icon(Icons.share_outlined), title: const Text('Teilen'), onTap: () => Navigator.pop(ctx, 'share')),
          ListTile(leading: const Icon(Icons.drive_file_move_outlined), title: const Text('Verschieben'), onTap: () => Navigator.pop(ctx, 'move')),
          ListTile(leading: const Icon(Icons.delete_outline), title: const Text('Löschen'), onTap: () => Navigator.pop(ctx, 'delete')),
        ]),
      ),
    );
    switch (action) {
      case 'rename':
        final name = await _askText('Umbenennen', it.name);
        if (name == null || name.isEmpty || name == it.name) return;
        final dest = uniquePath(p.dirname(it.path), name, it.folder ? '' : ext);
        if (it.folder) {
          await Directory(it.path).rename(dest);
        } else {
          await File(it.path).rename(dest);
          try {
            final d = await loadDocument(dest);
            d.title = name;
            await saveDocument(d, dest);
          } on Object {
            // the file name is what the library shows
          }
        }
      case 'dup':
        await File(it.path).copy(uniquePath(p.dirname(it.path), '${it.name} Kopie', ext));
      case 'share':
        await SharePlus.instance.share(ShareParams(files: [XFile(it.path)], title: it.name));
      case 'move':
        await _move(it);
      case 'delete':
        if (!mounted) return;
        final ok = await showDialog<bool>(
          context: context,
          builder: (ctx) => AlertDialog(
            title: Text('„${it.name}“ löschen?'),
            content: const Text('Das Element wird in den Papierkorb der Bibliothek verschoben.'),
            actions: [
              TextButton(onPressed: () => Navigator.pop(ctx, false), child: const Text('Abbrechen')),
              FilledButton(onPressed: () => Navigator.pop(ctx, true), child: const Text('Löschen')),
            ],
          ),
        );
        if (ok == true) {
          final trash = Directory(p.join(app.root, '.trash'));
          await trash.create(recursive: true);
          final dest = p.join(trash.path, '${DateTime.now().millisecondsSinceEpoch}-${p.basename(it.path)}');
          it.folder ? await Directory(it.path).rename(dest) : await File(it.path).rename(dest);
        }
    }
    if (mounted) setState(() {});
  }

  Future<void> _move(_Item it) async {
    final folders = <String>[app.root];
    for (final e in Directory(app.root).listSync(recursive: true)) {
      final rel = p.relative(e.path, from: app.root);
      if (e is Directory && !p.split(rel).any((s) => s.startsWith('.')) && !p.isWithin(it.path, e.path) && e.path != it.path) {
        folders.add(e.path);
      }
    }
    if (!mounted) return;
    final target = await showDialog<String>(
      context: context,
      builder: (ctx) => SimpleDialog(title: const Text('Verschieben nach'), children: [
        for (final f in folders)
          SimpleDialogOption(
            onPressed: () => Navigator.pop(ctx, f),
            child: Text(f == app.root ? 'Dokumente' : 'Dokumente / ${p.relative(f, from: app.root)}'),
          ),
      ]),
    );
    if (target == null || target == p.dirname(it.path)) return;
    final dest = uniquePath(target, it.name, it.folder ? '' : ext);
    it.folder ? await Directory(it.path).rename(dest) : await File(it.path).rename(dest);
  }

  // ------------------------------------------------------------------ build

  @override
  Widget build(BuildContext context) {
    final items = _items();
    final inSub = folder != app.root;
    final scheme = Theme.of(context).colorScheme;
    return PopScope(
      canPop: !inSub,
      onPopInvokedWithResult: (didPop, _) {
        if (!didPop && inSub) setState(() => folder = p.dirname(folder));
      },
      child: Scaffold(
        appBar: AppBar(
          leading: inSub
              ? IconButton(icon: const Icon(Icons.arrow_back), onPressed: () => setState(() => folder = p.dirname(folder)))
              : null,
          title: searching
              ? TextField(
                  autofocus: true,
                  decoration: const InputDecoration(hintText: 'Dokumente durchsuchen', border: InputBorder.none),
                  onChanged: (v) => setState(() => query = v),
                )
              : Text(inSub ? p.basename(folder) : 'Dokumente'),
          actions: [
            IconButton(
              icon: Icon(searching ? Icons.close : Icons.search),
              tooltip: 'Suchen',
              onPressed: () => setState(() {
                searching = !searching;
                query = '';
              }),
            ),
            if (app.sync.config.enabled)
              app.sync.running
                  ? const Padding(padding: EdgeInsets.all(14), child: SizedBox(width: 20, height: 20, child: CircularProgressIndicator(strokeWidth: 2)))
                  : IconButton(icon: const Icon(Icons.sync), tooltip: 'Synchronisieren', onPressed: () => app.sync.sync()),
            PopupMenuButton<String>(
              onSelected: (v) async {
                if (v == 'sort') setState(() => sortByName = !sortByName);
                if (v == 'settings') {
                  await Navigator.of(context).push(MaterialPageRoute<void>(builder: (_) => SettingsScreen(state: app)));
                  setState(() {});
                }
              },
              itemBuilder: (_) => [
                PopupMenuItem(value: 'sort', child: Text(sortByName ? 'Nach Datum sortieren' : 'Nach Name sortieren')),
                const PopupMenuItem(value: 'settings', child: Text('Einstellungen')),
              ],
            ),
          ],
        ),
        body: Column(children: [
          if (_importStatus != null)
            MaterialBanner(
              content: Text(_importStatus!),
              leading: const SizedBox(width: 20, height: 20, child: CircularProgressIndicator(strokeWidth: 2)),
              actions: [TextButton(onPressed: () => _cancelImport = true, child: const Text('Abbrechen'))],
            ),
          Expanded(
            child: items.isEmpty
                ? Center(
                    child: Padding(
                      padding: const EdgeInsets.all(32),
                      child: Column(mainAxisSize: MainAxisSize.min, children: [
                        Icon(Icons.menu_book_outlined, size: 72, color: scheme.outline),
                        const SizedBox(height: 16),
                        Text(query.isEmpty ? 'Noch keine Dokumente' : 'Nichts gefunden',
                            style: Theme.of(context).textTheme.titleLarge),
                        const SizedBox(height: 8),
                        const Text('Erstelle ein Notizbuch oder importiere PDF-, Markdown- oder .goodnotes-Dateien.',
                            textAlign: TextAlign.center),
                      ]),
                    ),
                  )
                : RefreshIndicator(
                    onRefresh: () async {
                      if (app.sync.config.enabled) await app.sync.sync();
                      setState(() {});
                    },
                    child: GridView.builder(
                      padding: const EdgeInsets.all(20),
                      gridDelegate: const SliverGridDelegateWithMaxCrossAxisExtent(
                          maxCrossAxisExtent: 180, mainAxisSpacing: 20, crossAxisSpacing: 18, childAspectRatio: 0.62),
                      itemCount: items.length,
                      itemBuilder: (ctx, i) => _card(items[i]),
                    ),
                  ),
          ),
        ]),
        floatingActionButton: FloatingActionButton.extended(
          onPressed: () => _addMenu(),
          icon: const Icon(Icons.add),
          label: const Text('Neu'),
        ),
      ),
    );
  }

  Future<void> _addMenu() async {
    final v = await showModalBottomSheet<String>(
      context: context,
      showDragHandle: true,
      builder: (ctx) => SafeArea(
        child: Column(mainAxisSize: MainAxisSize.min, children: [
          ListTile(leading: const Icon(Icons.menu_book_outlined), title: const Text('Notizbuch'), onTap: () => Navigator.pop(ctx, 'nb')),
          ListTile(leading: const Icon(Icons.create_new_folder_outlined), title: const Text('Ordner'), onTap: () => Navigator.pop(ctx, 'folder')),
          ListTile(
              leading: const Icon(Icons.file_open_outlined),
              title: const Text('Importieren…'),
              subtitle: const Text('PDF, Markdown, Bilder, .goodnotes, ZIP (Obsidian, Notion)'),
              onTap: () => Navigator.pop(ctx, 'import')),
          ListTile(
              leading: const Icon(Icons.folder_open_outlined),
              title: const Text('Obsidian-Vault / Markdown-Ordner…'),
              onTap: () => Navigator.pop(ctx, 'vault')),
        ]),
      ),
    );
    switch (v) {
      case 'nb':
        await _newNotebook();
      case 'folder':
        await _newFolder();
      case 'import':
        await _import();
      case 'vault':
        await _importFolder();
    }
  }

  Widget _card(_Item it) {
    final scheme = Theme.of(context).colorScheme;
    final text = Theme.of(context).textTheme;
    Widget cover;
    if (it.folder) {
      cover = Container(
        decoration: BoxDecoration(color: scheme.primaryContainer.withValues(alpha: 0.5), borderRadius: BorderRadius.circular(14)),
        child: Center(child: Icon(Icons.folder, size: 64, color: scheme.primary)),
      );
    } else {
      _loadCover(it);
      final (_, thumb, coverColor) = _covers[it.path] ?? (it.modified, null, null);
      cover = Container(
        decoration: BoxDecoration(
          color: Colors.white,
          borderRadius: const BorderRadius.horizontal(left: Radius.circular(4), right: Radius.circular(10)),
          boxShadow: const [BoxShadow(color: Color(0x38000000), blurRadius: 8, offset: Offset(0, 2))],
        ),
        clipBehavior: Clip.antiAlias,
        child: Stack(fit: StackFit.expand, children: [
          if (thumb != null) Image.memory(thumb, fit: BoxFit.cover, gaplessPlayback: true),
          if (coverColor != null)
            Positioned(left: 0, top: 0, bottom: 0, width: 9, child: ColoredBox(color: _parseHex(coverColor))),
        ]),
      );
    }
    return InkWell(
      borderRadius: BorderRadius.circular(14),
      onTap: () => it.folder ? setState(() => folder = it.path) : _open(it.path),
      onLongPress: () => _itemMenu(it),
      onSecondaryTap: () => _itemMenu(it),
      child: Padding(
        padding: const EdgeInsets.all(4),
        child: Column(children: [
          Expanded(child: cover),
          const SizedBox(height: 8),
          Text(it.name, maxLines: 1, overflow: TextOverflow.ellipsis, style: text.titleSmall),
          Text(it.folder ? 'Ordner' : _date(it.modified), style: text.bodySmall?.copyWith(color: scheme.outline)),
        ]),
      ),
    );
  }

  static Color _parseHex(String hex) {
    final v = int.tryParse(hex.replaceFirst('#', ''), radix: 16) ?? 0x3d5a80;
    return Color(0xff000000 | v);
  }

  static String _date(DateTime d) {
    final now = DateTime.now();
    String two(int v) => v.toString().padLeft(2, '0');
    if (d.year == now.year && d.month == now.month && d.day == now.day) return 'Heute, ${two(d.hour)}:${two(d.minute)}';
    return '${two(d.day)}.${two(d.month)}.${d.year}';
  }
}

class NewNotebookDialog extends StatefulWidget {
  const NewNotebookDialog({super.key});
  @override
  State<NewNotebookDialog> createState() => _NewNotebookDialogState();
}

class _NewNotebookDialogState extends State<NewNotebookDialog> {
  final title = TextEditingController(text: 'Unbenanntes Notizbuch');
  String size = 'a4', paper = 'lined', paperColor = 'white', cover = coverColors.first;

  static const papers = {'blank': 'Blanko', 'lined': 'Liniert', 'narrow': 'Eng liniert', 'grid': 'Kariert', 'dotted': 'Gepunktet'};
  static const sizes = {'a4': 'A4 Hochformat', 'a4-landscape': 'A4 Querformat', 'letter': 'US Letter', 'a5': 'A5'};
  static const colors = {'white': 'Weiß', 'yellow': 'Gelblich', 'gray': 'Grau', 'dark': 'Dunkel'};

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      title: const Text('Neues Notizbuch'),
      content: SizedBox(
        width: 420,
        child: SingleChildScrollView(
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, mainAxisSize: MainAxisSize.min, children: [
            TextField(controller: title, decoration: const InputDecoration(labelText: 'Titel')),
            const SizedBox(height: 16),
            DropdownButtonFormField<String>(
              initialValue: size,
              decoration: const InputDecoration(labelText: 'Format'),
              items: [for (final e in sizes.entries) DropdownMenuItem(value: e.key, child: Text(e.value))],
              onChanged: (v) => setState(() => size = v!),
            ),
            const SizedBox(height: 16),
            const Text('Umschlag'),
            Wrap(children: [
              for (final c in coverColors)
                InkWell(
                  onTap: () => setState(() => cover = c),
                  child: Container(
                    margin: const EdgeInsets.all(4),
                    width: 30,
                    height: 40,
                    decoration: BoxDecoration(
                      color: _LibraryScreenState._parseHex(c),
                      borderRadius: BorderRadius.circular(4),
                      border: cover == c ? Border.all(color: Theme.of(context).colorScheme.primary, width: 3) : null,
                    ),
                  ),
                ),
            ]),
            const SizedBox(height: 16),
            const Text('Papier'),
            Wrap(spacing: 8, children: [
              for (final e in papers.entries)
                ChoiceChip(
                  label: Text(e.value),
                  selected: paper == e.key,
                  onSelected: (_) => setState(() => paper = e.key),
                  avatar: _PaperPreview(kind: e.key, color: paperColors[paperColor]!),
                ),
            ]),
            const SizedBox(height: 8),
            Wrap(spacing: 8, children: [
              for (final e in colors.entries)
                ChoiceChip(label: Text(e.value), selected: paperColor == e.key, onSelected: (_) => setState(() => paperColor = e.key)),
            ]),
          ]),
        ),
      ),
      actions: [
        TextButton(onPressed: () => Navigator.pop(context), child: const Text('Abbrechen')),
        FilledButton(
          onPressed: () => Navigator.pop(context, (
            title.text.trim().isEmpty ? 'Unbenanntes Notizbuch' : title.text.trim(),
            size,
            paper,
            List<double>.of(paperColors[paperColor]!),
            cover,
          )),
          child: const Text('Erstellen'),
        ),
      ],
    );
  }
}

class _PaperPreview extends StatelessWidget {
  final String kind;
  final List<double> color;
  const _PaperPreview({required this.kind, required this.color});

  @override
  Widget build(BuildContext context) =>
      SizedBox(width: 18, height: 24, child: CustomPaint(painter: _PaperPainter(kind, color)));
}

class _PaperPainter extends CustomPainter {
  final String kind;
  final List<double> color;
  _PaperPainter(this.kind, this.color);

  @override
  void paint(Canvas canvas, Size size) {
    final (w, h) = pageSizes['a4']!;
    final page = Page(w, h, background: Background(kind: kind, color: color));
    canvas.scale(size.width / w, size.height / h);
    paintPaper(canvas, page);
  }

  @override
  bool shouldRepaint(_PaperPainter old) => old.kind != kind || old.color != color;
}
