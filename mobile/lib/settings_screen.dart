// Settings: Nextcloud sync, library location info.
import 'package:flutter/material.dart' hide Element, Page;

import 'main.dart';

class SettingsScreen extends StatefulWidget {
  final AppState state;
  const SettingsScreen({super.key, required this.state});

  @override
  State<SettingsScreen> createState() => _SettingsScreenState();
}

class _SettingsScreenState extends State<SettingsScreen> {
  late final cfg = widget.state.sync.config;
  late final server = TextEditingController(text: cfg.server);
  late final user = TextEditingController(text: cfg.user);
  late final password = TextEditingController(text: cfg.password);
  late final folder = TextEditingController(text: cfg.folder);
  String? status;
  bool busy = false;
  bool showPassword = false;

  Future<void> _apply() async {
    cfg
      ..server = server.text.trim()
      ..user = user.text.trim()
      ..password = password.text
      ..folder = folder.text.trim().isEmpty ? 'WellNotes' : folder.text.trim();
    await cfg.save();
    widget.state.sync.reschedule();
  }

  Future<void> _run(Future<String> Function() f) async {
    await _apply();
    setState(() {
      busy = true;
      status = 'Bitte warten …';
    });
    try {
      final msg = await f();
      setState(() => status = msg);
    } on Object catch (e) {
      setState(() => status = '$e');
    } finally {
      setState(() => busy = false);
    }
  }

  @override
  void dispose() {
    _apply();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    return Scaffold(
      appBar: AppBar(title: const Text('Einstellungen')),
      body: ListView(padding: const EdgeInsets.all(16), children: [
        Text('Synchronisation', style: text.titleLarge),
        const SizedBox(height: 4),
        const Text('Notizbücher werden mit deiner Nextcloud (oder einem anderen WebDAV-Server) in beide Richtungen '
            'abgeglichen – derselbe Ordner wie in der Linux-App. Bei Konflikten bleiben beide Versionen erhalten.'),
        SwitchListTile(
          title: const Text('Mit Nextcloud synchronisieren'),
          value: cfg.enabled,
          onChanged: (v) => setState(() => cfg.enabled = v),
        ),
        if (cfg.enabled) ...[
          TextField(
              controller: server,
              keyboardType: TextInputType.url,
              decoration: const InputDecoration(labelText: 'Server', hintText: 'https://cloud.example.org')),
          TextField(controller: user, decoration: const InputDecoration(labelText: 'Benutzername')),
          TextField(
            controller: password,
            obscureText: !showPassword,
            decoration: InputDecoration(
              labelText: 'App-Passwort',
              helperText: 'In Nextcloud unter Persönliche Einstellungen → Sicherheit anlegen',
              suffixIcon: IconButton(
                  icon: Icon(showPassword ? Icons.visibility_off : Icons.visibility),
                  onPressed: () => setState(() => showPassword = !showPassword)),
            ),
          ),
          TextField(controller: folder, decoration: const InputDecoration(labelText: 'Ordner in der Nextcloud')),
          SwitchListTile(
            title: const Text('Automatisch synchronisieren'),
            subtitle: const Text('Beim Start, nach dem Schließen eines Notizbuchs und regelmäßig'),
            value: cfg.auto,
            onChanged: (v) => setState(() => cfg.auto = v),
          ),
          ListTile(
            title: const Text('Intervall'),
            trailing: DropdownButton<int>(
              value: cfg.interval,
              items: [for (final m in [1, 5, 10, 15, 30, 60]) DropdownMenuItem(value: m, child: Text('$m min'))],
              onChanged: (v) => setState(() => cfg.interval = v ?? 5),
            ),
          ),
          const SizedBox(height: 8),
          Wrap(spacing: 8, children: [
            OutlinedButton(onPressed: busy ? null : () => _run(widget.state.sync.test), child: const Text('Verbindung testen')),
            FilledButton(
              onPressed: busy
                  ? null
                  : () => _run(() async {
                        await widget.state.sync.sync();
                        return widget.state.sync.lastMessage ?? 'Alles aktuell';
                      }),
              child: const Text('Jetzt synchronisieren'),
            ),
          ]),
          if (status != null) Padding(padding: const EdgeInsets.only(top: 12), child: Text(status!)),
        ],
        const Divider(height: 40),
        Text('Bibliothek', style: text.titleLarge),
        const SizedBox(height: 8),
        SelectableText(widget.state.root),
        const SizedBox(height: 4),
        const Text('Notizbücher sind normale .goodnotes-Dateien und per USB unter Android/data erreichbar.'),
        const Divider(height: 40),
        Text('Über', style: text.titleLarge),
        const SizedBox(height: 8),
        const Text('WellNotes für Android – handschriftliche Notizen, kompatibel mit .goodnotes und der Linux-App.\n'
            'Lizenz: GPL-3.0-or-later. Schriften: Noto (SIL Open Font License).'),
      ]),
    );
  }
}
