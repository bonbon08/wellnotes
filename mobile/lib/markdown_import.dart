// Markdown / Obsidian -> PDF pages (counterpart of wellnotes/markdown.py).
//
// Supports headings, paragraphs, emphasis, code, links, lists, task lists, quotes,
// code blocks, tables, rules, images; Obsidian wikilinks, embeds (images, notes,
// PDFs), callouts, ==highlights==, #tags, %%comments%%, footnotes and LaTeX
// (rendered by math.dart when available, otherwise shown as source).
import 'dart:io';
import 'dart:typed_data';

import 'package:flutter/services.dart' show rootBundle;
import 'package:markdown/markdown.dart' as md;
import 'package:path/path.dart' as p;
import 'package:pdf/pdf.dart';
import 'package:pdf/widgets.dart' as pw;

import 'math.dart' as math_render;

/// Finds files referenced by a note: relative to the note, then anywhere in the vault by name.
class VaultResolver {
  final String? root;
  String? baseDir;
  Map<String, String>? _index;
  VaultResolver(this.root, [this.baseDir]);

  static String? findVaultRoot(String path) {
    var d = Directory(path).existsSync() ? path : p.dirname(path);
    while (true) {
      if (Directory(p.join(d, '.obsidian')).existsSync()) return d;
      final parent = p.dirname(d);
      if (parent == d) return null;
      d = parent;
    }
  }

  void _build() {
    _index = {};
    final r = root ?? baseDir;
    if (r == null || !Directory(r).existsSync()) return;
    var n = 0;
    for (final e in Directory(r).listSync(recursive: true, followLinks: false)) {
      if (e is! File) continue;
      final rel = p.relative(e.path, from: r);
      if (p.split(rel).any((s) => s.startsWith('.'))) continue;
      final name = p.basename(e.path).toLowerCase();
      _index!.putIfAbsent(name, () => e.path);
      if (name.endsWith('.md')) _index!.putIfAbsent(name.substring(0, name.length - 3), () => e.path);
      if (++n > 50000) break;
    }
  }

  String? resolve(String target) {
    var t = target.split('|').first.split('#').first.trim();
    try {
      t = Uri.decodeFull(t);
    } on Object {
      // keep as is
    }
    if (t.isEmpty || t.contains('://')) return null;
    for (final base in [baseDir, root]) {
      if (base == null) continue;
      final cand = p.join(base, t);
      if (File(cand).existsSync()) return cand;
      if (File('$cand.md').existsSync()) return '$cand.md';
    }
    if (_index == null) _build();
    final name = p.basename(t).toLowerCase();
    return _index![name] ?? _index!['$name.md'];
  }
}

final _wikiEmbed = RegExp(r'!\[\[([^\]|#]+)(#[^\]|]*)?(?:\|([^\]]*))?\]\]');
final _wikiLink = RegExp(r'\[\[([^\]|#]*)(#[^\]|]*)?(?:\|([^\]]*))?\]\]');
const _imageExts = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp'};

/// `|` inside `$math$` or `code` in a table row is content, not a column separator.
String _protectTablePipes(String text) => text.split('\n').map((line) {
      if (!line.trimLeft().startsWith('|')) return line;
      line = line.replaceAllMapped(RegExp(r'`[^`]*`'), (m) => m[0]!.replaceAll('|', r'\|'));
      return line.replaceAllMapped(RegExp(r'(?<![\\$])\$[^$]+\$'), (m) => m[0]!.replaceAll('|', r'\vert '));
    }).join('\n');

String _prepare(String text) {
  text = text.replaceAll('\r\n', '\n');
  if (text.startsWith('---\n')) {
    final end = text.indexOf('\n---', 4);
    if (end != -1) text = text.substring(end + 4).replaceFirst(RegExp(r'^\n+'), '');
  }
  return _protectTablePipes(text.replaceAll(RegExp(r'%%.*?%%', dotAll: true), ''));
}

/// Rewrites Obsidian syntax into CommonMark the parser understands.
String _obsidianToMarkdown(String text, VaultResolver res, List<String> pdfEmbeds, int depth) {
  text = text.replaceAllMapped(_wikiEmbed, (m) {
    final target = m[1]!.trim();
    final path = res.resolve(target);
    final low = (path ?? target).toLowerCase();
    if (path != null && _imageExts.contains(p.extension(low))) {
      final size = RegExp(r'^(\d+)').firstMatch(m[3] ?? '')?.group(1);
      return '\n\n![${size ?? ''}](<${Uri.file(path)}>)\n\n';
    }
    if (path != null && low.endsWith('.pdf')) {
      pdfEmbeds.add(path);
      return '\n\n*→ PDF „${p.basename(path)}“ folgt als eigene Seiten*\n\n';
    }
    if (path != null && low.endsWith('.md') && depth < 2) {
      final inner = _prepare(File(path).readAsStringSync());
      final body = _obsidianToMarkdown(inner, res, pdfEmbeds, depth + 1);
      return '\n\n${body.split('\n').map((l) => '> $l').join('\n')}\n\n';
    }
    return '[${p.basename(target)}]';
  });
  text = text.replaceAllMapped(_wikiLink, (m) {
    final label = m[3] ?? ('${m[1]}${m[2] != null ? ' › ${m[2]!.substring(1)}' : ''}');
    return '[${label.isEmpty ? (m[2] ?? '').replaceFirst('#', '') : label}](wiki:)';
  });
  return text;
}

class _MarkSyntax extends md.InlineSyntax {
  _MarkSyntax() : super(r'==(?=\S)(.+?)(?<=\S)==');
  @override
  bool onMatch(md.InlineParser parser, Match match) {
    parser.addNode(md.Element.text('mark', match[1]!));
    return true;
  }
}

class _TagSyntax extends md.InlineSyntax {
  _TagSyntax() : super(r'(?<![\w&#/])#([^\W\d][\w/-]*)');
  @override
  bool onMatch(md.InlineParser parser, Match match) {
    parser.addNode(md.Element.text('tag', '#${match[1]}'));
    return true;
  }
}

class _MathSyntax extends md.InlineSyntax {
  _MathSyntax() : super(r'(?<![\\$])\$(?![\s$])([^$\n]+?)(?<!\s)\$(?![\d$])');
  @override
  bool onMatch(md.InlineParser parser, Match match) {
    parser.addNode(md.Element.text('math', match[1]!));
    return true;
  }
}

class _FootnoteRef extends md.InlineSyntax {
  _FootnoteRef() : super(r'\[\^([^\]\s]+)\]');
  @override
  bool onMatch(md.InlineParser parser, Match match) {
    parser.addNode(md.Element.text('sup', match[1]!));
    return true;
  }
}

class _Fonts {
  final pw.Font regular, bold, italic, boldItalic, mono;
  _Fonts(this.regular, this.bold, this.italic, this.boldItalic, this.mono);

  static _Fonts? _cached;
  static Future<_Fonts> load() async {
    if (_cached != null) return _cached!;
    Future<pw.Font> f(String n) async => pw.Font.ttf(await rootBundle.load('assets/fonts/$n'));
    return _cached = _Fonts(await f('NotoSans-Regular.ttf'), await f('NotoSans-Bold.ttf'),
        await f('NotoSans-Italic.ttf'), await f('NotoSans-BoldItalic.ttf'), await f('NotoSansMono-Regular.ttf'));
  }
}

const _callouts = <String, PdfColor>{
  'note': PdfColor(0.27, 0.47, 0.85), 'info': PdfColor(0.27, 0.47, 0.85), 'todo': PdfColor(0.27, 0.47, 0.85),
  'abstract': PdfColor(0.1, 0.65, 0.75), 'summary': PdfColor(0.1, 0.65, 0.75), 'tldr': PdfColor(0.1, 0.65, 0.75),
  'tip': PdfColor(0.1, 0.65, 0.6), 'hint': PdfColor(0.1, 0.65, 0.6), 'important': PdfColor(0.1, 0.65, 0.6),
  'success': PdfColor(0.2, 0.65, 0.3), 'check': PdfColor(0.2, 0.65, 0.3), 'done': PdfColor(0.2, 0.65, 0.3),
  'question': PdfColor(0.9, 0.6, 0.1), 'help': PdfColor(0.9, 0.6, 0.1), 'faq': PdfColor(0.9, 0.6, 0.1),
  'warning': PdfColor(0.93, 0.5, 0.1), 'caution': PdfColor(0.93, 0.5, 0.1), 'attention': PdfColor(0.93, 0.5, 0.1),
  'failure': PdfColor(0.85, 0.25, 0.25), 'danger': PdfColor(0.85, 0.2, 0.2), 'error': PdfColor(0.85, 0.2, 0.2),
  'bug': PdfColor(0.85, 0.2, 0.2), 'example': PdfColor(0.5, 0.35, 0.85), 'quote': PdfColor(0.55, 0.55, 0.6),
};

class _Builder {
  final _Fonts f;
  final VaultResolver res;
  final Map<String, math_render.MathImage?> maths;
  static const body = 10.5;
  static const ink = PdfColor(0.1, 0.1, 0.12);

  _Builder(this.f, this.res, this.maths);

  pw.TextStyle get base => pw.TextStyle(font: f.regular, fontBold: f.bold, fontItalic: f.italic,
      fontBoldItalic: f.boldItalic, fontSize: body, color: ink, lineSpacing: 2.5);

  // -- inline

  List<pw.InlineSpan> spans(List<md.Node>? nodes, pw.TextStyle style) {
    final out = <pw.InlineSpan>[];
    for (final n in nodes ?? const <md.Node>[]) {
      if (n is md.Text) {
        out.add(pw.TextSpan(text: n.text.replaceAll('\n', ' '), style: style));
      } else if (n is md.Element) {
        switch (n.tag) {
          case 'strong':
            out.addAll(spans(n.children, style.copyWith(fontWeight: pw.FontWeight.bold)));
          case 'em':
            out.addAll(spans(n.children, style.copyWith(fontStyle: pw.FontStyle.italic)));
          case 'del':
            out.addAll(spans(n.children, style.copyWith(decoration: pw.TextDecoration.lineThrough)));
          case 'code':
            out.add(pw.TextSpan(
                text: ' ${n.textContent} ',
                style: style.copyWith(font: f.mono, fontSize: (style.fontSize ?? body) * 0.92,
                    background: const pw.BoxDecoration(color: PdfColor(0.93, 0.94, 0.95)))));
          case 'a':
            out.addAll(spans(n.children,
                style.copyWith(color: const PdfColor(0.1, 0.39, 0.78), decoration: pw.TextDecoration.underline)));
          case 'mark':
            out.add(pw.TextSpan(
                text: n.textContent,
                style: style.copyWith(background: const pw.BoxDecoration(color: PdfColor(1, 0.95, 0.66)))));
          case 'tag':
            out.add(pw.TextSpan(
                text: ' ${n.textContent} ',
                style: style.copyWith(color: const PdfColor(0.42, 0.27, 0.76),
                    background: const pw.BoxDecoration(color: PdfColor(0.94, 0.91, 0.98)))));
          case 'sup':
            out.add(pw.TextSpan(text: n.textContent, style: style.copyWith(fontSize: (style.fontSize ?? body) * 0.7),
                baseline: (style.fontSize ?? body) * 0.35));
          case 'math':
            final img = maths[n.textContent];
            if (img != null) {
              final h = img.height * (style.fontSize ?? body) / math_render.mathFontSize;
              out.add(pw.WidgetSpan(
                  child: pw.Image(pw.MemoryImage(img.png), height: h),
                  baseline: -img.depth * (style.fontSize ?? body) / math_render.mathFontSize));
            } else {
              out.add(pw.TextSpan(text: n.textContent, style: style.copyWith(fontStyle: pw.FontStyle.italic)));
            }
          case 'br':
            out.add(pw.TextSpan(text: '\n', style: style));
          case 'img':
            out.add(pw.TextSpan(text: '[${n.attributes['alt'] ?? 'Bild'}]', style: style));
          case 'input':
            out.add(pw.TextSpan(text: n.attributes['checked'] != null ? '☑ ' : '☐ ', style: style));
          default:
            out.addAll(spans(n.children, style));
        }
      }
    }
    return out;
  }

  pw.Widget? image(md.Element img, double maxWidth) {
    final src = img.attributes['src'] ?? '';
    Uint8List? data;
    try {
      if (src.startsWith('file:')) {
        data = File(Uri.parse(src).toFilePath()).readAsBytesSync();
      } else if (src.startsWith('data:')) {
        data = UriData.parse(src).contentAsBytes();
      } else if (!src.contains('://')) {
        final path = res.resolve(src);
        if (path != null) data = File(path).readAsBytesSync();
      }
    } on Object {
      data = null;
    }
    if (data == null) {
      return pw.Text('[Bild: ${img.attributes['alt'] ?? src}]',
          style: base.copyWith(fontStyle: pw.FontStyle.italic, color: PdfColors.grey600));
    }
    final width = double.tryParse(img.attributes['alt'] ?? '');
    try {
      // Centre with a Row: a Container with alignment would grow to the full page height.
      return pw.Padding(
        padding: const pw.EdgeInsets.symmetric(vertical: 6),
        child: pw.Row(mainAxisAlignment: pw.MainAxisAlignment.center, children: [
          pw.ConstrainedBox(
            constraints: pw.BoxConstraints(maxWidth: width != null ? width * 0.75 : maxWidth, maxHeight: 600),
            child: pw.Image(pw.MemoryImage(data), fit: pw.BoxFit.contain),
          ),
        ]),
      );
    } on Object {
      return null;
    }
  }

  // -- blocks

  List<pw.Widget> blocks(List<md.Node> nodes, {double width = 483}) {
    final out = <pw.Widget>[];
    for (final n in nodes) {
      if (n is md.Text) {
        if (n.text.trim().isNotEmpty) out.add(pw.RichText(text: pw.TextSpan(children: spans([n], base))));
        continue;
      }
      if (n is! md.Element) continue;
      switch (n.tag) {
        case 'h1' || 'h2' || 'h3' || 'h4' || 'h5' || 'h6':
          final level = int.parse(n.tag.substring(1));
          final size = const [22.0, 18.0, 15.0, 13.0, 12.0, 11.0][level - 1];
          out.add(pw.Container(
            margin: pw.EdgeInsets.only(top: level <= 2 ? 12 : 8, bottom: 6),
            padding: const pw.EdgeInsets.only(bottom: 3),
            decoration: level <= 2
                ? const pw.BoxDecoration(border: pw.Border(bottom: pw.BorderSide(color: PdfColor(0.85, 0.86, 0.89))))
                : null,
            child: pw.RichText(
                text: pw.TextSpan(
                    children: spans(n.children, base.copyWith(fontSize: size, fontWeight: pw.FontWeight.bold)))),
          ));
        case 'p':
          out.addAll(_paragraph(n, width));
        case 'ul' || 'ol':
          out.add(_list(n, width));
        case 'blockquote':
          out.add(_quote(n, width));
        case 'pre':
          final code = n.textContent.replaceAll(RegExp(r'\n$'), '');
          final lang = (n.children?.firstOrNull is md.Element)
              ? ((n.children!.first as md.Element).attributes['class'] ?? '')
              : '';
          if (lang.contains('language-math') && maths[code] != null) {
            out.add(_mathBlock(code));
          } else {
            out.add(pw.Container(
              width: double.infinity,
              margin: const pw.EdgeInsets.symmetric(vertical: 6),
              padding: const pw.EdgeInsets.all(8),
              color: const PdfColor(0.95, 0.955, 0.965),
              child: pw.Text(code, style: base.copyWith(font: f.mono, fontSize: body - 1.5, lineSpacing: 1.5)),
            ));
          }
        case 'mathblock':
          out.add(_mathBlock(n.textContent));
        case 'table':
          out.add(_table(n));
        case 'hr':
          out.add(pw.Divider(color: const PdfColor(0.8, 0.81, 0.84), thickness: 1.2, height: 20));
        default:
          out.addAll(blocks(n.children ?? const [], width: width));
      }
    }
    return out;
  }

  pw.Widget _mathBlock(String tex) {
    final img = maths['\$\$$tex'];
    if (img == null) {
      return pw.Container(
          padding: const pw.EdgeInsets.all(8),
          color: const PdfColor(0.95, 0.955, 0.965),
          child: pw.Text(tex, style: base.copyWith(font: f.mono, fontSize: body - 1.5)));
    }
    return pw.Padding(
      padding: const pw.EdgeInsets.symmetric(vertical: 6),
      child: pw.Row(mainAxisAlignment: pw.MainAxisAlignment.center, children: [
        pw.Image(pw.MemoryImage(img.png), height: img.height * (body + 1.5) / math_render.mathFontSize),
      ]),
    );
  }

  List<pw.Widget> _paragraph(md.Element n, double width) {
    // Images inside a paragraph become blocks of their own.
    final out = <pw.Widget>[];
    var run = <md.Node>[];
    void flush() {
      if (run.any((x) => x is! md.Text || x.text.trim().isNotEmpty)) {
        out.add(pw.Padding(
            padding: const pw.EdgeInsets.only(bottom: 8),
            child: pw.RichText(text: pw.TextSpan(children: spans(run, base)))));
      }
      run = [];
    }

    for (final c in n.children ?? const <md.Node>[]) {
      if (c is md.Element && c.tag == 'img') {
        flush();
        final w = image(c, width);
        if (w != null) out.add(w);
      } else {
        run.add(c);
      }
    }
    flush();
    return out;
  }

  pw.Widget _list(md.Element n, double width) {
    final ordered = n.tag == 'ol';
    var k = int.tryParse(n.attributes['start'] ?? '1') ?? 1;
    final items = <pw.Widget>[];
    for (final li in (n.children ?? const <md.Node>[]).whereType<md.Element>()) {
      final children = li.children ?? const <md.Node>[];
      final checkbox = children.whereType<md.Element>().where((e) => e.tag == 'input').firstOrNull;
      final rest = children.where((c) => c != checkbox).toList();
      final inlineOnly = rest.every((c) => c is md.Text || (c is md.Element && !_blockTags.contains(c.tag)));
      final content = inlineOnly
          ? [pw.RichText(text: pw.TextSpan(children: spans(rest, base)))]
          : blocks(rest, width: width - 22);
      final marker = checkbox != null
          ? pw.Container(
              width: 9,
              height: 9,
              margin: const pw.EdgeInsets.only(top: 3),
              decoration: pw.BoxDecoration(
                  border: pw.Border.all(color: ink, width: 0.8),
                  color: checkbox.attributes['checked'] != null ? const PdfColor(0.18, 0.45, 0.85) : null))
          : pw.Text(ordered ? '${k++}.' : '•', style: base);
      items.add(pw.Padding(
        padding: const pw.EdgeInsets.only(bottom: 3),
        child: pw.Row(crossAxisAlignment: pw.CrossAxisAlignment.start, children: [
          pw.SizedBox(width: 22, child: pw.Align(alignment: pw.Alignment.topLeft, child: marker)),
          pw.Expanded(child: pw.Column(crossAxisAlignment: pw.CrossAxisAlignment.start, children: content)),
        ]),
      ));
    }
    return pw.Padding(
        padding: const pw.EdgeInsets.only(bottom: 6),
        child: pw.Column(crossAxisAlignment: pw.CrossAxisAlignment.start, children: items));
  }

  static const _blockTags = {'p', 'ul', 'ol', 'blockquote', 'pre', 'table', 'hr', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6'};

  pw.Widget _quote(md.Element n, double width) {
    var children = n.children ?? const <md.Node>[];
    PdfColor bar = const PdfColor(0.78, 0.8, 0.84);
    pw.Widget? title;
    // Obsidian callout: first paragraph starts with "[!type] Title", the rest of it is the body.
    final first = children.whereType<md.Element>().firstOrNull;
    final lead = first != null && first.tag == 'p' && (first.children?.firstOrNull is md.Text)
        ? first.children!.first as md.Text
        : null;
    final m = lead == null ? null : RegExp(r'^\[!(\w+)\][+-]?[ \t]*([^\n]*)\n?').firstMatch(lead.text);
    if (m != null) {
      final kind = m[1]!.toLowerCase();
      bar = _callouts[kind] ?? const PdfColor(0.27, 0.47, 0.85);
      final heading = m[2]!.trim();
      title = pw.Padding(
          padding: const pw.EdgeInsets.only(bottom: 4),
          child: pw.Text(heading.isEmpty ? kind[0].toUpperCase() + kind.substring(1) : heading,
              style: base.copyWith(fontWeight: pw.FontWeight.bold, color: bar)));
      final restLead = lead!.text.substring(m.end);
      final bodyNodes = <md.Node>[
        if (restLead.trim().isNotEmpty) md.Text(restLead),
        ...first!.children!.skip(1),
      ];
      final hasBody = bodyNodes.any((n) => n is! md.Text || n.text.trim().isNotEmpty);
      children = [
        if (hasBody) md.Element('p', bodyNodes),
        ...children.skip(1),
      ];
    }
    return pw.Container(
      margin: const pw.EdgeInsets.symmetric(vertical: 4),
      padding: const pw.EdgeInsets.only(left: 12, top: 2, bottom: 2),
      decoration: pw.BoxDecoration(border: pw.Border(left: pw.BorderSide(color: bar, width: 3))),
      child: pw.Column(crossAxisAlignment: pw.CrossAxisAlignment.start, children: [
        ?title,
        ...blocks(children, width: width - 15),
      ]),
    );
  }

  pw.Widget _table(md.Element n) {
    final rows = <pw.TableRow>[];
    var header = true;
    for (final section in (n.children ?? const <md.Node>[]).whereType<md.Element>()) {
      for (final tr in (section.children ?? const <md.Node>[]).whereType<md.Element>()) {
        final cells = <pw.Widget>[];
        for (final td in (tr.children ?? const <md.Node>[]).whereType<md.Element>()) {
          final img = (td.children ?? const []).whereType<md.Element>().where((e) => e.tag == 'img').firstOrNull;
          cells.add(pw.Padding(
            padding: const pw.EdgeInsets.all(5),
            child: img != null
                ? (image(img, 160) ?? pw.SizedBox())
                : pw.RichText(
                    text: pw.TextSpan(
                        children: spans(td.children,
                            base.copyWith(fontSize: body - 0.5, fontWeight: header ? pw.FontWeight.bold : null)))),
          ));
        }
        rows.add(pw.TableRow(
            decoration: header ? const pw.BoxDecoration(color: PdfColor(0.94, 0.95, 0.97)) : null, children: cells));
        header = false;
      }
    }
    return pw.Padding(
      padding: const pw.EdgeInsets.symmetric(vertical: 6),
      child: pw.Table(border: pw.TableBorder.all(color: const PdfColor(0.82, 0.83, 0.86), width: 0.8), children: rows),
    );
  }
}

/// Collects all formulas so they can be rendered (asynchronously) before typesetting.
Set<String> _collectMath(String text) {
  final out = <String>{};
  for (final m in RegExp(r'\$\$(.+?)\$\$', dotAll: true).allMatches(text)) {
    out.add('\$\$${m[1]!.trim()}');
  }
  final noBlocks = text.replaceAll(RegExp(r'\$\$.+?\$\$', dotAll: true), '');
  for (final m in RegExp(r'(?<![\\$])\$(?![\s$])([^$\n]+?)(?<!\s)\$(?![\d$])').allMatches(noBlocks)) {
    out.add(m[1]!);
  }
  return out;
}

/// Typesets Markdown into PDF bytes.  Embedded PDFs are appended to [pdfEmbeds].
Future<Uint8List> markdownToPdf(String text, {String? baseDir, VaultResolver? resolver, List<String>? pdfEmbeds,
    double width = 595.28, double height = 841.89}) async {
  final res = resolver ?? VaultResolver(baseDir != null ? VaultResolver.findVaultRoot(baseDir) : null, baseDir);
  res.baseDir = baseDir ?? res.baseDir;
  final embeds = pdfEmbeds ?? <String>[];
  var src = _obsidianToMarkdown(_prepare(text), res, embeds, 0);
  // Display math becomes a fenced block so the parser leaves it alone.
  src = src.replaceAllMapped(RegExp(r'\$\$(.+?)\$\$', dotAll: true), (m) => '\n\n```math\n${m[1]!.trim()}\n```\n\n');
  final maths = <String, math_render.MathImage?>{};
  for (final tex in _collectMath(_prepare(text))) {
    final display = tex.startsWith('\$\$');
    final body = display ? tex.substring(2) : tex;
    final img = await math_render.renderMath(body, display: display);
    maths[tex] = img;
    if (display) maths[body] = img; // looked up by the fenced block's content
  }
  final nodes = md.Document(
    extensionSet: md.ExtensionSet.gitHubFlavored,
    inlineSyntaxes: [_MathSyntax(), _MarkSyntax(), _TagSyntax(), _FootnoteRef()],
    encodeHtml: false,
  ).parseLines(src.split('\n'));
  final fonts = await _Fonts.load();
  final b = _Builder(fonts, res, maths);
  final doc = pw.Document();
  doc.addPage(pw.MultiPage(
    pageFormat: PdfPageFormat(width, height),
    margin: const pw.EdgeInsets.fromLTRB(56, 60, 56, 60),
    theme: pw.ThemeData.withFont(base: fonts.regular, bold: fonts.bold, italic: fonts.italic,
        boldItalic: fonts.boldItalic),
    maxPages: 500,
    build: (ctx) {
      final blocks = b.blocks(nodes, width: width - 112);
      return blocks.isEmpty ? [pw.SizedBox()] : blocks;
    },
  ));
  return doc.save();
}
