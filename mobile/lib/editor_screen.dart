// Notebook screen: toolbar, canvas, page overview, autosave, export (counterpart of wellnotes/editor.py).
import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:isolate';
import 'dart:ui' as ui;

import 'package:file_picker/file_picker.dart';
import 'package:flutter/material.dart' hide Element, Page;
import 'package:flutter/services.dart';
import 'package:flutter_svg/flutter_svg.dart';
import 'package:path/path.dart' as p;
import 'package:pdf/pdf.dart' as pdf;
import 'package:pdf/widgets.dart' as pw;
import 'package:share_plus/share_plus.dart';

import 'importers.dart';
import 'main.dart';
import 'model.dart';
import 'notebook_view.dart';
import 'render.dart';
import 'storage.dart';
import 'tools.dart';
import 'widgets.dart';

class EditorScreen extends StatefulWidget {
  final AppState state;
  final Document doc;
  const EditorScreen({super.key, required this.state, required this.doc});

  @override
  State<EditorScreen> createState() => _EditorScreenState();
}

class _EditorScreenState extends State<EditorScreen> with WidgetsBindingObserver {
  final GlobalKey<NotebookViewState> _view = GlobalKey();
  Timer? _saveTimer;
  bool _saving = false;
  bool _fullscreen = false;
  int _page = 0;
  DateTime? _backPressed;
  int _changeCounter = 0;

  Document get doc => widget.doc;
  ToolSettings get tools => widget.state.tools;
  NotebookViewState? get view => _view.currentState;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    tools.addListener(_onTools);
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    tools.removeListener(_onTools);
    _saveTimer?.cancel();
    if (_fullscreen) SystemChrome.setEnabledSystemUIMode(SystemUiMode.edgeToEdge);
    super.dispose();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState s) {
    // Save immediately when the app goes to the background (Android may kill it).
    if (s == AppLifecycleState.paused || s == AppLifecycleState.inactive) _save();
  }

  void _onTools() => setState(() {});

  void _toast(String msg) => ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(msg)));

  // ------------------------------------------------------------------ saving

  void _onChanged() {
    _changeCounter++;
    setState(() {});
    _saveTimer?.cancel();
    _saveTimer = Timer(const Duration(milliseconds: 1500), _save);
  }

  Future<void> _save() async {
    final path = doc.path;
    if (path == null || !doc.dirty || _saving) return;
    _saving = true;
    final counter = _changeCounter;
    try {
      final thumb = doc.pages.isEmpty ? null : await pageThumbnail(doc.pages.first, view?.res ?? Resources(doc));
      // Snapshot (elements are immutable) and write in a background isolate.
      final snap = Document.fromJson(doc.toJson(), Map.of(doc.attachments));
      await Isolate.run(() => saveDocument(snap, path, thumbnail: thumb));
      if (counter == _changeCounter) doc.dirty = false;
    } on Object catch (e) {
      if (mounted) _toast('Speichern fehlgeschlagen: $e');
    } finally {
      _saving = false;
    }
    if (doc.dirty) _onChanged();
  }

  Future<void> _close() async {
    view?.commitText();
    _saveTimer?.cancel();
    if (doc.dirty) {
      _saving = false;
      await _save();
    }
    if (mounted) Navigator.of(context).pop();
  }

  // ------------------------------------------------------------------ pages

  void _structure(void Function() change, {int? focus}) {
    final token = doc.begin(structure: true);
    change();
    doc.commit(token);
    view?.relayout();
    _onChanged();
    if (focus != null) WidgetsBinding.instance.addPostFrameCallback((_) => view?.scrollToPage(focus));
  }

  void addPage({bool after = true, int? index}) {
    final cur = index ?? view?.currentPage ?? 0;
    final like = doc.pages[cur];
    final page = like.blankCopy();
    if (page.background.kind == 'pdf') page.background = Background();
    final pos = after ? cur + 1 : cur;
    _structure(() => doc.pages.insert(pos, page), focus: pos);
  }

  void duplicatePage([int? index]) {
    final cur = index ?? view?.currentPage ?? 0;
    _structure(() => doc.pages.insert(cur + 1, doc.pages[cur].duplicate()), focus: cur + 1);
  }

  void deletePage([int? index]) {
    if (doc.pages.length <= 1) return;
    final cur = index ?? view?.currentPage ?? 0;
    view?.clearSelection();
    _structure(() => doc.pages.removeAt(cur), focus: cur > 0 ? cur - 1 : 0);
    _toast('Seite gelöscht – mit Rückgängig wiederherstellbar');
  }

  void movePage(int i, int delta) {
    final j = i + delta;
    if (j < 0 || j >= doc.pages.length) return;
    _structure(() => doc.pages.insert(j, doc.pages.removeAt(i)), focus: j);
  }

  void _insertPages(List<Page> pages) {
    final pos = (view?.currentPage ?? 0) + 1;
    _structure(() => doc.pages.insertAll(pos, pages), focus: pos);
  }

  void _undo() {
    final pages = doc.undo();
    if (pages == null) return;
    view?.clearSelection();
    view?.relayout();
    view?.invalidate();
    _onChanged();
  }

  void _redo() {
    final pages = doc.redo();
    if (pages == null) return;
    view?.clearSelection();
    view?.relayout();
    view?.invalidate();
    _onChanged();
  }

  Future<void> _changeTemplate() async {
    final page = doc.pages[view?.currentPage ?? 0];
    var kind = page.background.kind == 'pdf' ? 'blank' : page.background.kind;
    var color = List<double>.of(page.background.color);
    final all = await showDialog<bool>(
      context: context,
      builder: (ctx) => StatefulBuilder(
        builder: (ctx, set) => AlertDialog(
          title: const Text('Papiervorlage'),
          content: Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.start, children: [
            Wrap(spacing: 8, children: [
              for (final e in const {'blank': 'Blanko', 'lined': 'Liniert', 'narrow': 'Eng liniert', 'grid': 'Kariert', 'dotted': 'Gepunktet'}.entries)
                ChoiceChip(label: Text(e.value), selected: kind == e.key, onSelected: (_) => set(() => kind = e.key)),
            ]),
            const SizedBox(height: 8),
            Wrap(spacing: 8, children: [
              for (final e in const {'white': 'Weiß', 'yellow': 'Gelblich', 'gray': 'Grau', 'dark': 'Dunkel'}.entries)
                ChoiceChip(
                    label: Text(e.value),
                    selected: color.take(3).toList().toString() == paperColors[e.key]!.toString(),
                    onSelected: (_) => set(() => color = List.of(paperColors[e.key]!))),
            ]),
          ]),
          actions: [
            TextButton(onPressed: () => Navigator.pop(ctx), child: const Text('Abbrechen')),
            TextButton(onPressed: () => Navigator.pop(ctx, false), child: const Text('Diese Seite')),
            FilledButton(onPressed: () => Navigator.pop(ctx, true), child: const Text('Alle Seiten')),
          ],
        ),
      ),
    );
    if (all == null) return;
    for (final pg in all ? doc.pages : [page]) {
      if (pg.background.kind == 'pdf') continue;
      pg.background
        ..kind = kind
        ..color = color;
      pg.touch();
    }
    doc.dirty = true;
    view?.invalidate();
    _onChanged();
  }

  // ------------------------------------------------------------------ files

  Future<(String, Uint8List)?> _pick(List<String> exts) async {
    final files = await FilePicker.pickFiles(type: FileType.custom, allowedExtensions: exts);
    if (files.isEmpty) return null;
    return (files.first.name, await files.first.readAsBytes());
  }

  Future<void> _insertImage() async {
    final f = await _pick(['png', 'jpg', 'jpeg', 'webp', 'gif', 'bmp']);
    if (f == null) return;
    final att = doc.addAttachment(f.$2);
    final (iw, ih) = await imageSize(f.$2);
    await view?.insertImage(att, iw, ih, (page, cx, cy) => fitImage(att, iw, ih, page, cx: cx, cy: cy, maxFrac: 0.6));
    tools.update(() => tools.tool = 'lasso');
  }

  Future<void> _insertPdf() async {
    final f = await _pick(['pdf']);
    if (f == null) return;
    _insertPages(await pdfPages(doc, f.$2));
  }

  Future<void> _insertMarkdown() async {
    final f = await _pick(['md', 'markdown', 'txt']);
    if (f == null) return;
    _toast('Markdown wird gesetzt …');
    _insertPages(await markdownPages(doc, _utf8(f.$2)));
  }

  static String _utf8(Uint8List d) {
    try {
      return utf8.decode(d);
    } on FormatException {
      return String.fromCharCodes(d);
    }
  }

  Future<void> _exportPdf() async {
    _toast('PDF wird erstellt …');
    final res = view?.res ?? Resources(doc);
    final out = pw.Document();
    for (final page in doc.pages) {
      final img = await renderPageImage(page, res, 2.0);
      final png = await img.toByteData(format: ui.ImageByteFormat.png);
      img.dispose();
      if (png == null) continue;
      out.addPage(pw.Page(
        pageFormat: pdf.PdfPageFormat(page.width / gnScale, page.height / gnScale),
        margin: pw.EdgeInsets.zero,
        build: (_) => pw.Image(pw.MemoryImage(png.buffer.asUint8List()), fit: pw.BoxFit.fill),
      ));
    }
    final dir = await Directory.systemTemp.createTemp('wellnotes-export-');
    final path = p.join(dir.path, '${doc.title}.pdf');
    await File(path).writeAsBytes(await out.save());
    await SharePlus.instance.share(ShareParams(files: [XFile(path, mimeType: 'application/pdf')], title: doc.title));
  }

  Future<void> _shareGoodnotes() async {
    await _save();
    if (doc.path != null) {
      await SharePlus.instance.share(ShareParams(files: [XFile(doc.path!)], title: doc.title));
    }
  }

  Future<void> _rename() async {
    final ctl = TextEditingController(text: doc.title);
    final name = await showDialog<String>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Notizbuch umbenennen'),
        content: TextField(controller: ctl, autofocus: true),
        actions: [
          TextButton(onPressed: () => Navigator.pop(ctx), child: const Text('Abbrechen')),
          FilledButton(onPressed: () => Navigator.pop(ctx, ctl.text.trim()), child: const Text('Umbenennen')),
        ],
      ),
    );
    if (name == null || name.isEmpty || name == doc.title || doc.path == null) return;
    final dest = uniquePath(p.dirname(doc.path!), name, ext);
    await File(doc.path!).rename(dest);
    widget.state.openPaths
      ..remove(doc.path)
      ..add(dest);
    doc
      ..path = dest
      ..title = name
      ..dirty = true;
    _onChanged();
  }

  void _toggleFullscreen() {
    setState(() => _fullscreen = !_fullscreen);
    SystemChrome.setEnabledSystemUIMode(_fullscreen ? SystemUiMode.immersiveSticky : SystemUiMode.edgeToEdge);
  }

  // ------------------------------------------------------------------ page overview

  Future<void> _pagesSheet() async {
    final res = view?.res ?? Resources(doc);
    await showModalBottomSheet<void>(
      context: context,
      isScrollControlled: true,
      showDragHandle: true,
      builder: (ctx) => SizedBox(
        height: MediaQuery.of(ctx).size.height * 0.7,
        child: GridView.builder(
          padding: const EdgeInsets.all(16),
          gridDelegate: const SliverGridDelegateWithMaxCrossAxisExtent(
              maxCrossAxisExtent: 150, childAspectRatio: 0.62, mainAxisSpacing: 12, crossAxisSpacing: 12),
          itemCount: doc.pages.length,
          itemBuilder: (ctx, i) => InkWell(
            onTap: () {
              Navigator.pop(ctx);
              view?.scrollToPage(i);
            },
            onLongPress: () async {
              Navigator.pop(ctx);
              await _pageMenu(i);
            },
            child: Column(children: [
              Expanded(
                child: Container(
                  decoration: BoxDecoration(
                    border: Border.all(
                        color: i == _page ? Theme.of(ctx).colorScheme.primary : Colors.black12, width: i == _page ? 3 : 1),
                  ),
                  child: _Thumb(page: doc.pages[i], res: res),
                ),
              ),
              const SizedBox(height: 4),
              Text('${i + 1}'),
            ]),
          ),
        ),
      ),
    );
  }

  Future<void> _pageMenu(int i) async {
    final v = await showModalBottomSheet<String>(
      context: context,
      showDragHandle: true,
      builder: (ctx) => SafeArea(
        child: Column(mainAxisSize: MainAxisSize.min, children: [
          ListTile(title: Text('Seite ${i + 1}', style: Theme.of(ctx).textTheme.titleMedium)),
          for (final (key, label, icon) in [
            ('after', 'Seite danach einfügen', Icons.note_add_outlined),
            ('before', 'Seite davor einfügen', Icons.note_add_outlined),
            ('dup', 'Duplizieren', Icons.copy_outlined),
            ('up', 'Nach vorne', Icons.arrow_upward),
            ('down', 'Nach hinten', Icons.arrow_downward),
            ('del', 'Löschen', Icons.delete_outline),
          ])
            ListTile(leading: Icon(icon), title: Text(label), onTap: () => Navigator.pop(ctx, key)),
        ]),
      ),
    );
    switch (v) {
      case 'after':
        addPage(index: i);
      case 'before':
        addPage(after: false, index: i);
      case 'dup':
        duplicatePage(i);
      case 'up':
        movePage(i, -1);
      case 'down':
        movePage(i, 1);
      case 'del':
        deletePage(i);
    }
  }

  // ------------------------------------------------------------------ build

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return PopScope(
      canPop: false,
      onPopInvokedWithResult: (didPop, _) {
        if (didPop) return;
        // A stray back swipe must not close the notebook: require a second press.
        if (_fullscreen) {
          _toggleFullscreen();
          return;
        }
        final now = DateTime.now();
        if (_backPressed != null && now.difference(_backPressed!) < const Duration(seconds: 2)) {
          _close();
        } else {
          _backPressed = now;
          _toast('Zum Schließen noch einmal „Zurück“ – oder den Pfeil oben links verwenden');
        }
      },
      child: Scaffold(
        appBar: _fullscreen
            ? null
            : AppBar(
                leading: IconButton(icon: const Icon(Icons.arrow_back), tooltip: 'Zurück zur Bibliothek', onPressed: _close),
                title: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                  Text(doc.title, overflow: TextOverflow.ellipsis),
                  Text('Seite ${_page + 1} von ${doc.pages.length}', style: Theme.of(context).textTheme.bodySmall),
                ]),
                actions: _headerActions(),
              ),
        body: SafeArea(
          top: _fullscreen,
          child: Column(children: [
            Material(
              elevation: 1,
              color: scheme.surfaceContainer,
              child: SizedBox(height: 52, child: _toolbar()),
            ),
            Expanded(
              child: Stack(children: [
                NotebookView(
                  key: _view,
                  doc: doc,
                  tools: tools,
                  onChanged: _onChanged,
                  onPageChanged: (i) => setState(() => _page = i),
                  onSelectionChanged: () => setState(() {}),
                ),
                Positioned(
                  bottom: 16,
                  left: 0,
                  right: 0,
                  child: Center(child: (view?.hasSelection ?? false) ? _selectionBar() : _pagePill()),
                ),
              ]),
            ),
          ]),
        ),
      ),
    );
  }

  List<Widget> _headerActions() => [
        IconButton(icon: const Icon(Icons.undo), tooltip: 'Rückgängig', onPressed: doc.canUndo ? _undo : null),
        IconButton(icon: const Icon(Icons.redo), tooltip: 'Wiederholen', onPressed: doc.canRedo ? _redo : null),
        IconButton(icon: const Icon(Icons.grid_view_outlined), tooltip: 'Seitenübersicht', onPressed: _pagesSheet),
        IconButton(icon: const Icon(Icons.note_add_outlined), tooltip: 'Neue Seite', onPressed: () => addPage()),
        IconButton(icon: const Icon(Icons.fullscreen), tooltip: 'Vollbild', onPressed: _toggleFullscreen),
        PopupMenuButton<String>(
          onSelected: (v) async {
            switch (v) {
              case 'template':
                await _changeTemplate();
              case 'pdf':
                await _insertPdf();
              case 'md':
                await _insertMarkdown();
              case 'img':
                await _insertImage();
              case 'export':
                await _exportPdf();
              case 'share':
                await _shareGoodnotes();
              case 'rename':
                await _rename();
              case 'finger':
                tools.update(() => tools.fingerDraw = !tools.fingerDraw);
                await tools.save();
              case 'fit':
                view?.zoomFit();
              case 'paste':
                view?.paste();
              case 'delpage':
                deletePage();
            }
          },
          itemBuilder: (_) => [
            const PopupMenuItem(value: 'template', child: Text('Papiervorlage ändern…')),
            const PopupMenuItem(value: 'delpage', child: Text('Aktuelle Seite löschen')),
            const PopupMenuDivider(),
            const PopupMenuItem(value: 'pdf', child: Text('PDF einfügen…')),
            const PopupMenuItem(value: 'md', child: Text('Markdown einfügen…')),
            const PopupMenuItem(value: 'img', child: Text('Bild einfügen…')),
            if (elementClipboard.isNotEmpty) const PopupMenuItem(value: 'paste', child: Text('Einfügen')),
            const PopupMenuDivider(),
            CheckedPopupMenuItem(value: 'finger', checked: tools.fingerDraw, child: const Text('Mit dem Finger zeichnen')),
            const PopupMenuItem(value: 'fit', child: Text('An Breite anpassen')),
            const PopupMenuItem(value: 'rename', child: Text('Umbenennen…')),
            const PopupMenuDivider(),
            const PopupMenuItem(value: 'export', child: Text('Als PDF exportieren / teilen')),
            const PopupMenuItem(value: 'share', child: Text('.goodnotes teilen')),
          ],
        ),
      ];

  Widget _toolButton(String tool, String icon, String tip) {
    final scheme = Theme.of(context).colorScheme;
    final selected = tools.tool == tool;
    return IconButton(
      tooltip: tip,
      style: IconButton.styleFrom(backgroundColor: selected ? scheme.secondaryContainer : null),
      onPressed: () {
        view?.commitText();
        if (tool != 'lasso') view?.clearSelection();
        tools.update(() => tools.tool = tool);
      },
      icon: SvgPicture.asset('assets/icons/$icon',
          width: 22, height: 22, colorFilter: ColorFilter.mode(selected ? scheme.primary : scheme.onSurface, BlendMode.srcIn)),
    );
  }

  Widget _toolbar() {
    final t = tools.tool;
    return SingleChildScrollView(
      scrollDirection: Axis.horizontal,
      padding: const EdgeInsets.symmetric(horizontal: 8),
      child: Row(children: [
        if (_fullscreen) ...[
          IconButton(icon: const Icon(Icons.fullscreen_exit), tooltip: 'Vollbild verlassen', onPressed: _toggleFullscreen),
          IconButton(icon: const Icon(Icons.undo), onPressed: doc.canUndo ? _undo : null),
          IconButton(icon: const Icon(Icons.redo), onPressed: doc.canRedo ? _redo : null),
          const VerticalDivider(),
        ],
        _toolButton('pen', 'wn-pen-symbolic.svg', 'Stift'),
        _toolButton('highlighter', 'wn-highlighter-symbolic.svg', 'Textmarker'),
        _toolButton('eraser', 'wn-eraser-symbolic.svg', 'Radierer'),
        _toolButton('shape', 'wn-shapes-symbolic.svg', 'Formen'),
        _toolButton('lasso', 'wn-lasso-symbolic.svg', 'Lasso'),
        _toolButton('text', 'wn-text-symbolic.svg', 'Text'),
        IconButton(
          tooltip: 'Bild einfügen',
          onPressed: _insertImage,
          icon: SvgPicture.asset('assets/icons/wn-image-symbolic.svg',
              width: 22, height: 22, colorFilter: ColorFilter.mode(Theme.of(context).colorScheme.onSurface, BlendMode.srcIn)),
        ),
        const SizedBox(height: 28, child: VerticalDivider()),
        ..._options(t),
        if (_fullscreen) ...[
          const SizedBox(height: 28, child: VerticalDivider()),
          Text('${_page + 1} / ${doc.pages.length}'),
          IconButton(icon: const Icon(Icons.note_add_outlined), onPressed: () => addPage()),
        ],
      ]),
    );
  }

  List<Widget> _options(String t) {
    if (t == 'lasso') return [const Text('Elemente umkreisen, um sie auszuwählen')];
    if (t == 'eraser') {
      return [
        SegmentedButton<bool>(
          segments: const [ButtonSegment(value: false, label: Text('Ganzer Strich')), ButtonSegment(value: true, label: Text('Präzise'))],
          selected: {tools.eraserPrecise},
          showSelectedIcon: false,
          onSelectionChanged: (s) => tools.update(() => tools.eraserPrecise = s.first),
        ),
        const SizedBox(width: 8),
        SizeControl(key: const ValueKey('size-eraser'), tools: tools, tool: 'eraser'),
      ];
    }
    final palette = t == 'highlighter' ? hlColors : penColors;
    final current = tools.color(t);
    final isCustom = !palette.any((c) => c.toString() == current.toString());
    return [
      if (t == 'pen')
        SegmentedButton<String>(
          segments: const [ButtonSegment(value: 'fountain', label: Text('Füller')), ButtonSegment(value: 'ball', label: Text('Kuli'))],
          selected: {tools.penStyle},
          showSelectedIcon: false,
          onSelectionChanged: (s) => tools.update(() => tools.penStyle = s.first),
        ),
      const SizedBox(width: 6),
      for (final c in palette)
        ColorDot(color: c, selected: c.toString() == current.toString(), onTap: () => _setColor(t, List.of(c))),
      ColorDot(
        color: isCustom ? current : const [0.5, 0.5, 0.5, 1],
        selected: isCustom,
        onTap: () async {
          final c = await pickColor(context, current);
          if (c != null) _setColor(t, c);
        },
      ),
      const SizedBox(height: 28, child: VerticalDivider()),
      SizeControl(key: ValueKey('size-$t'), tools: tools, tool: t),
      if (t == 'text')
        FilterChip(
          label: const Text('Auf Linien'),
          selected: tools.textOnLines,
          onSelected: (v) {
            tools.update(() => tools.textOnLines = v);
            tools.save();
          },
        ),
    ];
  }

  void _setColor(String t, List<double> c) {
    tools.setColor(t, c);
    if (t == 'text') view?.setState(() {});
  }

  Widget _pagePill() => IgnorePointer(
        child: Container(
          padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 4),
          decoration: BoxDecoration(color: Colors.black54, borderRadius: BorderRadius.circular(999)),
          child: Text('${_page + 1} / ${doc.pages.length}', style: const TextStyle(color: Colors.white, fontSize: 13)),
        ),
      );

  Widget _selectionBar() {
    final v = view!;
    return Material(
      elevation: 4,
      borderRadius: BorderRadius.circular(28),
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 6),
        child: Row(mainAxisSize: MainAxisSize.min, children: [
          IconButton(
              icon: const Icon(Icons.content_cut),
              tooltip: 'Ausschneiden',
              onPressed: () {
                v.copySelection();
                v.deleteSelection();
              }),
          IconButton(icon: const Icon(Icons.copy), tooltip: 'Kopieren', onPressed: () {
            v.copySelection();
            _toast('Kopiert – über das Menü einfügen');
          }),
          IconButton(icon: const Icon(Icons.control_point_duplicate), tooltip: 'Duplizieren', onPressed: v.duplicateSelection),
          IconButton(icon: const Icon(Icons.delete_outline), tooltip: 'Löschen', onPressed: v.deleteSelection),
          const SizedBox(height: 28, child: VerticalDivider()),
          for (final c in penColors) ColorDot(color: c, selected: false, size: 18, onTap: () => v.recolorSelection(c)),
        ]),
      ),
    );
  }
}

/// Lazily rendered page thumbnail for the page overview.
class _Thumb extends StatefulWidget {
  final Page page;
  final Resources res;
  const _Thumb({required this.page, required this.res});
  @override
  State<_Thumb> createState() => _ThumbState();
}

class _ThumbState extends State<_Thumb> {
  ui.Image? image;

  @override
  void initState() {
    super.initState();
    renderPageImage(widget.page, widget.res, 240 / widget.page.width).then((img) {
      if (mounted) {
        setState(() => image = img);
      } else {
        img.dispose();
      }
    });
  }

  @override
  void dispose() {
    image?.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => image == null
      ? ColoredBox(color: rgba(widget.page.background.color, 1))
      : RawImage(image: image, fit: BoxFit.contain);
}
