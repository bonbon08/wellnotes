import 'dart:io';

import 'package:flutter/material.dart' hide Element, Page;
import 'package:path/path.dart' as p;
import 'package:path_provider/path_provider.dart';

import 'library_screen.dart';
import 'sync.dart';
import 'tools.dart';

/// App-wide state: library location, tool settings, sync and open notebooks.
class AppState {
  final String root;
  final ToolSettings tools;
  final SyncManager sync;
  final Set<String> openPaths = {};
  AppState(this.root, this.tools, this.sync);
}

Future<String> libraryRoot() async {
  // App-specific external storage is visible over USB (Android/data/<app>/files) and needs no permission.
  Directory? base;
  try {
    base = await getExternalStorageDirectory();
  } on Object {
    base = null;
  }
  base ??= await getApplicationDocumentsDirectory();
  final dir = Directory(p.join(base.path, 'WellNotes'));
  await dir.create(recursive: true);
  return dir.path;
}

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  final root = await libraryRoot();
  final tools = await ToolSettings.load();
  late final AppState state;
  final sync = SyncManager(() => root, () => state.openPaths);
  state = AppState(root, tools, sync);
  await sync.init();
  runApp(WellNotesApp(state));
}

class WellNotesApp extends StatelessWidget {
  final AppState state;
  const WellNotesApp(this.state, {super.key});

  @override
  Widget build(BuildContext context) {
    ThemeData theme(Brightness b) => ThemeData(
          colorScheme: ColorScheme.fromSeed(seedColor: const Color(0xff3d5a80), brightness: b),
          useMaterial3: true,
          fontFamily: 'NotoSans',
        );
    return MaterialApp(
      title: 'WellNotes',
      debugShowCheckedModeBanner: false,
      theme: theme(Brightness.light),
      darkTheme: theme(Brightness.dark),
      home: LibraryScreen(state: state),
    );
  }
}
