// Imports: PDF, images, Markdown, .goodnotes, Obsidian vaults / Markdown folders / Notion exports (ZIP).
import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:archive/archive.dart';
import 'package:path/path.dart' as p;

import 'markdown_import.dart';
import 'model.dart';
import 'storage.dart';

const importExts = {'.pdf', '.md', '.markdown', '.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp', '.goodnotes', '.zip'};

String uniquePath(String folder, String name, [String ext = '']) {
  name = name.replaceAll('/', '-').trim();
  if (name.isEmpty) name = 'Unbenannt';
  var path = p.join(folder, '$name$ext');
  var n = 2;
  while (File(path).existsSync() || Directory(path).existsSync()) {
    path = p.join(folder, '$name $n$ext');
    n++;
  }
  return path;
}

Future<List<Page>> markdownPages(Document doc, String text, {String? baseDir, VaultResolver? resolver}) async {
  final embeds = <String>[];
  final (w, h) = pageSizes['a4']!;
  final data = await markdownToPdf(text, baseDir: baseDir, resolver: resolver, pdfEmbeds: embeds,
      width: w / gnScale, height: h / gnScale);
  final pages = await pdfPages(doc, data);
  for (final path in embeds.toSet()) {
    try {
      pages.addAll(await pdfPages(doc, await File(path).readAsBytes()));
    } on Object {
      // unreadable PDF: keep the note itself
    }
  }
  return pages;
}

/// Creates a notebook from a single file; returns its path in [folder].
Future<String> importFile(String path, String folder) async {
  final ext = p.extension(path).toLowerCase();
  final title = titleFromPath(path);
  if (ext == '.goodnotes') {
    final dest = uniquePath(folder, title, '.goodnotes');
    await File(path).copy(dest);
    return dest;
  }
  final data = await File(path).readAsBytes();
  final doc = Document(title);
  if (ext == '.pdf') {
    doc.pages = await pdfPages(doc, data);
  } else if (ext == '.md' || ext == '.markdown') {
    doc.pages = await markdownPages(doc, _utf8(data), baseDir: p.dirname(path));
  } else {
    doc.pages = [await imagePage(doc, data)];
  }
  if (doc.pages.isEmpty) throw const FormatException('Keine Seiten gefunden');
  final dest = uniquePath(folder, title, '.goodnotes');
  await saveDocument(doc, dest);
  return dest;
}

String _utf8(List<int> data) {
  try {
    return const Utf8Decoder().convert(data);
  } on FormatException {
    return String.fromCharCodes(data);
  }
}

/// Imports a folder tree of Markdown/PDF files (Obsidian, Logseq, Joplin …) into a new library folder.
Future<(String, int, List<String>)> importFolder(String src, String destParent,
    {String? name, void Function(int done, int total, String current)? progress, bool Function()? cancelled}) async {
  final vault = VaultResolver.findVaultRoot(p.join(src, 'x')) ?? src;
  final files = <String>[];
  for (final e in Directory(src).listSync(recursive: true, followLinks: false)) {
    if (e is! File) continue;
    final rel = p.relative(e.path, from: src);
    final parts = p.split(rel);
    if (parts.any((s) => s.startsWith('.') || s == 'node_modules' || s == '_resources')) continue;
    final low = rel.toLowerCase();
    if (low.endsWith('.md') || low.endsWith('.markdown') || low.endsWith('.pdf')) files.add(rel);
  }
  files.sort();
  // PDFs embedded in a note are imported with that note, not separately.
  final embedded = <String>{};
  for (final rel in files.where((f) => !f.toLowerCase().endsWith('.pdf'))) {
    final text = _utf8(await File(p.join(src, rel)).readAsBytes());
    for (final m in RegExp(r'!\[\[([^\]|#]+\.pdf)', caseSensitive: false).allMatches(text)) {
      embedded.add(p.basename(m[1]!).toLowerCase());
    }
  }
  files.removeWhere((f) => f.toLowerCase().endsWith('.pdf') && embedded.contains(p.basename(f).toLowerCase()));
  final dest = uniquePath(destParent, titleFromPath(name ?? src));
  await Directory(dest).create(recursive: true);
  final resolver = VaultResolver(vault);
  final errors = <String>[];
  var done = 0;
  for (final rel in files) {
    if (cancelled?.call() ?? false) break;
    progress?.call(done, files.length, rel);
    final srcPath = p.join(src, rel);
    final sub = p.dirname(rel);
    final outDir = sub == '.' ? dest : p.joinAll([dest, ...p.split(sub).map(titleFromPath)]);
    await Directory(outDir).create(recursive: true);
    final title = titleFromPath(srcPath);
    try {
      final doc = Document(title);
      if (rel.toLowerCase().endsWith('.pdf')) {
        doc.pages = await pdfPages(doc, await File(srcPath).readAsBytes());
      } else {
        resolver.baseDir = p.dirname(srcPath);
        doc.pages = await markdownPages(doc, _utf8(await File(srcPath).readAsBytes()),
            baseDir: p.dirname(srcPath), resolver: resolver);
      }
      if (doc.pages.isNotEmpty) {
        await saveDocument(doc, uniquePath(outDir, title, '.goodnotes'));
        done++;
      }
    } on Object catch (e) {
      errors.add('$rel: $e');
    }
  }
  progress?.call(files.length, files.length, '');
  return (dest, done, errors);
}

/// Imports a ZIP export (Obsidian vault zipped, Notion "Markdown & CSV", …).
Future<(String, int, List<String>)> importZip(String path, String destParent,
    {void Function(int, int, String)? progress, bool Function()? cancelled}) async {
  final tmp = await Directory.systemTemp.createTemp('wellnotes-import-');
  try {
    Future<void> extract(Uint8List bytes, String into) async {
      for (final f in ZipDecoder().decodeBytes(bytes).files) {
        if (!f.isFile) continue;
        final out = p.normalize(p.join(into, f.name));
        if (!p.isWithin(into, out)) continue; // ignore entries escaping the folder
        await Directory(p.dirname(out)).create(recursive: true);
        if (f.name.toLowerCase().endsWith('.zip')) {
          await extract(f.content, p.dirname(out)); // Notion nests "Part-N.zip" archives
        } else {
          await File(out).writeAsBytes(f.content);
        }
      }
    }

    await extract(await File(path).readAsBytes(), tmp.path);
    var root = tmp.path;
    final entries = Directory(root).listSync().where((e) => !p.basename(e.path).startsWith('.')).toList();
    if (entries.length == 1 && entries.first is Directory) root = entries.first.path;
    return await importFolder(root, destParent,
        name: p.basenameWithoutExtension(path), progress: progress, cancelled: cancelled);
  } finally {
    await tmp.delete(recursive: true);
  }
}
