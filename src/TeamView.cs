using System;
using System.Collections.Generic;
using System.Drawing;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading.Tasks;
using System.Windows.Forms;

namespace RyvenPanel
{
    /// <summary>Owner area: install the team gateway, create username/password accounts (stored on the server), map ranks to panel rights,
    /// approve IPs, audit log.</summary>
    class TeamView : Panel
    {
        readonly Func<Ssh> getSsh;
        readonly Func<bool> sshReady;
        readonly Action<string, Color> setStatus;
        readonly Func<string> craftyToken;
        public Action<int> PendingChanged;
        public Func<FirewallView> GetFirewall;

        Label hSub;
        TabBtn[] tabs;
        Panel[] pages;
        Panel setup, body;
        ProgressLine progress;
        Field fPort;
        Label setupInfo;
        ListView rankList, accList, reqList, logList;
        FlowLayoutPanel permBox;
        Label permTitle, addrLbl;
        TextBox blocked;
        readonly Dictionary<string, CheckBox> permChecks = new Dictionary<string, CheckBox>();
        Dictionary<string, object> state;
        Dictionary<string, List<string>> roles = new Dictionary<string, List<string>>();
        string selRank, thumb;
        int port = 8444;
        bool busy, installed;
        readonly Timer poll = new Timer { Interval = 20000 };

        const string Py = "python3 /opt/ryvenpanel/gateway.py";

        public TeamView(Func<Ssh> getSsh, Func<bool> sshReady, Action<string, Color> setStatus, Func<string> craftyToken)
        {
            this.getSsh = getSsh;
            this.sshReady = sshReady;
            this.setStatus = setStatus;
            this.craftyToken = craftyToken;
            Dock = DockStyle.Fill;
            BackColor = Theme.Bg;
            Build();
            poll.Tick += async (s, e) => { if (Visible && !busy) await Reload(false); };
        }

        public async void Activate()
        {
            if (!sshReady())
            {
                hSub.Text = "Für den Team-Zugang braucht die App die SSH-Verbindung – richte sie einmal unter „Docker“ ein.";
                hSub.ForeColor = Theme.Yellow;
                return;
            }
            poll.Start();
            await Reload(true);
        }

        public void Deactivate() { poll.Stop(); }

        /// <summary>Quietly checks for pending IP requests (for the sidebar badge).</summary>
        public async Task CheckPending()
        {
            if (!sshReady()) return;
            var r = await getSsh().Run("[ -f /opt/ryvenpanel/gateway.py ] && " + Py + " admin state 2>/dev/null || true", 30000);
            var d = Parse(r.Out);
            if (d != null && PendingChanged != null) PendingChanged(J.L(J.Get(d, "requests")).Count());
            if (d != null) await EnsureCurrent(d);
        }

        bool updateTried;

        /// <summary>The gateway ships inside this exe – if the server runs an older version, replace it (once per app start).</summary>
        async Task<bool> EnsureCurrent(Dictionary<string, object> st)
        {
            var want = Regex.Match(GatewaySource(), "^VERSION = \"([^\"]+)\"", RegexOptions.Multiline).Groups[1].Value;
            var have = J.S(st, "version") ?? "";
            if (updateTried || want.Length == 0 || have == want) return false;
            updateTried = true;
            var script =
                "set -e\ncd /opt/ryvenpanel\n" +
                "printf '%s\\n' " + Ssh.B64(GatewaySource()) + " > gateway.py.new\n" +
                "python3 -m py_compile gateway.py.new\n" +
                "cp -p gateway.py gateway.py.v" + Regex.Replace(have, "[^0-9.]", "") + ".bak\n" +
                "printf '%s\\n' " + Ssh.B64(EcoSource()) + " > eco.py.new && mv eco.py.new eco.py\n" +
                "mv gateway.py.new gateway.py\n" +
                "systemctl restart ryvenpanel-gateway\nsleep 2\nsystemctl is-active ryvenpanel-gateway\n";
            var r = await getSsh().Run(script, 60000);
            if (r.Ok && r.Out.Contains("active"))
            {
                setStatus("Team-Dienst auf Version " + want + " aktualisiert (vorher " + (have.Length > 0 ? have : "?") + ").", Theme.Green);
                return true;
            }
            setStatus("Team-Dienst konnte nicht aktualisiert werden: " + r.Message, Theme.Red);
            return false;
        }

        // ───────────────────────────── Layout ─────────────────────────────
        void Build()
        {
            var header = Ui.Header("Team & Zugänge", out hSub, new[] { "Rechte", "Accounts", "IP-Freigaben", "Protokoll", "Einstellungen" }, out tabs, i => SetTab(i));
            progress = new ProgressLine { Dock = DockStyle.Top, BackColor = Theme.Bg };
            body = new Panel { Dock = DockStyle.Fill, BackColor = Theme.Bg, Padding = new Padding(0, 12, 0, 0), Visible = false };
            pages = new[] { BuildRights(), BuildAccounts(), BuildRequests(), BuildLog(), BuildSettings() };
            foreach (var p in pages) body.Controls.Add(p);
            BuildSetup();
            Controls.Add(body);
            Controls.Add(setup);
            Controls.Add(progress);
            Controls.Add(header);
            SetTab(0);
        }

        void SetTab(int i) { for (int k = 0; k < pages.Length; k++) { pages[k].Visible = k == i; tabs[k].Active = k == i; } }

        void BuildSetup()
        {
            setup = new Panel { Dock = DockStyle.Fill, BackColor = Theme.Bg, AutoScroll = true, Visible = false, Padding = new Padding(0, 12, 0, 0) };
            var card = new RoundPanel { Location = new Point(0, 12), Size = new Size(760, 640), Padding = new Padding(26, 20, 26, 20) };
            setup.Controls.Add(card);
            setup.Resize += (s, e) => card.Width = Math.Max(Theme.Px(520), Math.Min(Theme.Px(820), setup.ClientSize.Width - 4));
            int y = 20;
            Action<string, Font, Color, int> text = (t, f, c, h) =>
            {
                var l = Theme.Lbl(t, f, c, Color.Transparent);
                l.Location = new Point(26, y); l.MaximumSize = new Size(700, 0);
                card.Controls.Add(l);
                y += h;
            };
            text("Team-Zugang einrichten", Theme.F(15f, FontStyle.Bold), Theme.Text, 40);
            text("Teammitglieder melden sich mit Benutzername und Passwort an. Die Accounts legst du hier an – gespeichert wird alles\n" +
                 "nur auf dem RyvenMC-Server (Passwörter verschlüsselt als Hash). Jeder Account wird an die IP seines ersten Logins\n" +
                 "gebunden – neue IPs musst du freigeben. Das Team bekommt nie deinen SSH-Schlüssel oder Crafty-Zugang;\n" +
                 "ein kleiner Dienst auf dem Server prüft jede Aktion.",
                 Theme.F(9.25f), Theme.Text2, 86);
            text("1.  Dienst installieren", Theme.F(10f, FontStyle.Bold), Theme.Text, 28);
            var l2 = Theme.Lbl("PORT", Theme.F(8.25f, FontStyle.Bold), Theme.Muted, Color.Transparent); l2.Location = new Point(26, y);
            fPort = new Field(false) { Location = new Point(26, y + 20), Size = new Size(100, 38) };
            fPort.Box.Text = "8444";
            card.Controls.AddRange(new Control[] { l2, fPort });
            y += 72;
            var bInstall = new FlatBtn("Team-Zugang installieren", Theme.Accent) { Location = new Point(26, y), Size = new Size(260, 42) };
            bInstall.Click += async (s, e) => await Install(fPort.Box.Text.Trim());
            card.Controls.Add(bInstall);
            y += 58;
            text("2.  Accounts anlegen", Theme.F(10f, FontStyle.Bold), Theme.Text, 24);
            text("Danach unter „Accounts“ für jedes Teammitglied Benutzername + Startpasswort vergeben. Rechte kommen entweder\n" +
                 "vom LuckPerms-Rang eines verknüpften Minecraft-Spielers oder von einem festen Rang. Beim ersten Login muss\n" +
                 "jedes Mitglied ein eigenes Passwort festlegen.",
                 Theme.F(9f), Theme.Muted, 60);
            setupInfo = Theme.Lbl("", Theme.F(9f), Theme.Muted, Color.Transparent);
            setupInfo.Location = new Point(26, y); setupInfo.MaximumSize = new Size(700, 0);
            card.Controls.Add(setupInfo);
            card.Height = y + 60;
        }

        Panel BuildRights()
        {
            var p = new Panel { Dock = DockStyle.Fill, BackColor = Theme.Bg };
            var split = new SplitContainer { Dock = DockStyle.Fill, BackColor = Theme.Bg, SplitterWidth = 12 };
            split.Panel1.BackColor = split.Panel2.BackColor = Theme.Bg;
            UiX.Ratio(split, 0.42, 280, 360);
            rankList = Ui.List(new[] { "Rang (LuckPerms)", "Gewicht", "Panel-Rechte" }, new[] { 160, 70, 200 });
            var c1 = new RoundPanel { Dock = DockStyle.Fill, Padding = new Padding(8, 10, 8, 8) };
            c1.Controls.Add(rankList);
            split.Panel1.Controls.Add(c1);

            var c2 = new RoundPanel { Dock = DockStyle.Fill, Padding = new Padding(18, 14, 14, 12) };
            permTitle = Theme.Lbl("Rang links auswählen", Theme.F(12f, FontStyle.Bold), Theme.Text, Color.Transparent);
            permTitle.Dock = DockStyle.Top; permTitle.AutoSize = false; permTitle.Height = 34;
            var hint = Theme.Lbl("Ein Spieler bekommt die Rechte seines höchsten Rangs (nach Gewicht), für den hier etwas freigegeben ist.\n" +
                                 "Ränge ohne Haken haben keinen Zugang. Änderungen gelten sofort – angemeldete Mitglieder sehen neue Bereiche nach spätestens 1 Minute.",
                                 Theme.F(8.75f), Theme.Muted, Color.Transparent);
            hint.Dock = DockStyle.Top; hint.AutoSize = false; hint.Height = 42;
            permBox = new FlowLayoutPanel { Dock = DockStyle.Fill, FlowDirection = FlowDirection.TopDown, WrapContents = false, AutoScroll = true, BackColor = Theme.Card, Padding = new Padding(0, 8, 0, 0) };
            var bar = new FlowLayoutPanel { Dock = DockStyle.Bottom, Height = 46, BackColor = Theme.Card, Padding = new Padding(0, 6, 0, 0) };
            var bSave = new FlatBtn("Rechte speichern", Theme.Accent) { Height = 34, Margin = new Padding(0, 0, 8, 0) };
            var bAll = new FlatBtn("Alles", Theme.CardHi) { Height = 34, Margin = new Padding(0, 0, 8, 0) };
            var bRead = new FlatBtn("Nur lesen", Theme.CardHi) { Height = 34, Margin = new Padding(0, 0, 8, 0) }.Tip("Server sehen + Konsole lesen");
            var bNone = new FlatBtn("Kein Zugang", Theme.CardHi) { Height = 34, Margin = new Padding(0, 0, 8, 0) };
            bar.Controls.AddRange(new Control[] { bSave, bAll, bRead, bNone });
            c2.Controls.Add(permBox);
            c2.Controls.Add(bar);
            c2.Controls.Add(hint);
            c2.Controls.Add(permTitle);
            split.Panel2.Controls.Add(c2);
            p.Controls.Add(split);

            rankList.SelectedIndexChanged += (s, e) => { var r = Ui.Sel(rankList); if (r != null) ShowRank(r); };
            Action<Func<string, bool>> setAll = f => { foreach (var kv in permChecks) kv.Value.Checked = f(kv.Key); };
            bAll.Click += (s, e) => setAll(k => true);
            bRead.Click += (s, e) => setAll(k => k == "servers.view" || k == "console.view");
            bNone.Click += (s, e) => setAll(k => false);
            bSave.Click += async (s, e) =>
            {
                if (selRank == null) return;
                roles[selRank] = permChecks.Where(kv => kv.Value.Checked).Select(kv => kv.Key).ToList();
                if (roles[selRank].Contains("console.command.unrestricted") && !Dialogs.Confirm(FindForm(), "Achtung",
                        "„Alle Konsolenbefehle“ erlaubt auch op, lp, stop usw. – damit kann sich der Rang selbst alle Rechte geben.\n\nWirklich freigeben?")) return;
                await Admin("roles", roles.Where(kv => kv.Value.Count > 0).ToDictionary(kv => kv.Key, kv => (object)kv.Value), "Rechte für „" + selRank + "“ gespeichert");
            };
            return p;
        }

        Panel BuildAccounts()
        {
            var p = new Panel { Dock = DockStyle.Fill, BackColor = Theme.Bg };
            var bNew = Ui.Btn("+ Account anlegen", Theme.Accent);
            var bEdit = Ui.Btn("Bearbeiten", Theme.CardHi).Tip("Verknüpften Spieler oder festen Rang ändern");
            var bPw = Ui.Btn("Passwort zurücksetzen", Theme.CardHi).Tip("Neues Startpasswort vergeben – das Mitglied muss es beim nächsten Login ändern");
            var bIp = Ui.Btn("IPs ▾", Theme.CardHi).Tip("Weitere IP freigeben oder eine entfernen");
            var bKick = Ui.Btn("Abmelden erzwingen", Theme.CardHi);
            var bFreeze = Ui.Btn("Sperren / Entsperren", Theme.Mix(Theme.Red, Theme.Bg, 0.35));
            var bDel = Ui.Btn("Löschen", Theme.Mix(Theme.Red, Theme.Bg, 0.35));
            var bar = Ui.Bar(46, bNew, bEdit, bPw, bIp, bKick, bFreeze, bDel);
            accList = Ui.List(new[] { "Benutzer", "Rechte von", "Rang", "Freigegebene IPs", "Letzter Login", "Status" }, new[] { 130, 170, 100, 200, 130, 130 });
            var c = new RoundPanel { Dock = DockStyle.Fill, Padding = new Padding(8, 10, 8, 8) };
            c.Controls.Add(accList);
            p.Controls.Add(c);
            p.Controls.Add(bar);
            Func<object> sel = () => Ui.SelData<object>(accList);
            Func<object, string> name = a => J.S(a, "username");
            Func<object, string, Dictionary<string, object>> act = (a, action) => new Dictionary<string, object> { { "username", name(a) }, { "action", action } };

            bNew.Click += async (s, e) => await EditAccount(null);
            bEdit.Click += async (s, e) => { var a = sel(); if (a != null) await EditAccount(a); };
            accList.DoubleClick += async (s, e) => { var a = sel(); if (a != null) await EditAccount(a); };
            bPw.Click += async (s, e) =>
            {
                var a = sel(); if (a == null) return;
                var pw = Dialogs.Ask(FindForm(), "Passwort zurücksetzen", "Neues Startpasswort für „" + name(a) + "“ (beim nächsten Login muss ein eigenes gewählt werden):", NewPassword());
                if (pw == null) return;
                var d = act(a, "password"); d["password"] = pw;
                if (await Admin("user", d, "Passwort von " + name(a) + " zurückgesetzt")) ShowCredentials(name(a), pw);
            };
            bIp.Click += (s, e) =>
            {
                var a = sel(); if (a == null) return;
                var m = Ui.Menu();
                m.Item("+ Weitere IP freigeben …", async () =>
                {
                    var ip = Dialogs.Ask(FindForm(), "IP freigeben", "Zusätzliche IP für " + name(a) + ":", "");
                    if (ip == null || !Regex.IsMatch(ip.Trim(), @"^[0-9a-fA-F:.]{3,45}$")) return;
                    await Admin("ip", new Dictionary<string, object> { { "username", name(a) }, { "ip", ip.Trim() }, { "action", "add" } }, "IP freigegeben");
                });
                var ips = J.L(J.Parse(J.S(a, "ips") ?? "[]")).Select(Convert.ToString).ToList();
                if (ips.Count > 0) m.Sep();
                foreach (var ip in ips)
                {
                    var x = ip;
                    m.Item("Entfernen: " + x, async () => await Admin("ip", new Dictionary<string, object> { { "username", name(a) }, { "ip", x }, { "action", "remove" } }, "IP entfernt"));
                }
                Ui.DropDown(bIp, m);
            };
            bKick.Click += async (s, e) => { var a = sel(); if (a != null) await Admin("user", act(a, "kick"), "Sitzung beendet"); };
            bFreeze.Click += async (s, e) =>
            {
                var a = sel(); if (a == null) return;
                bool frozen = J.N(a, "frozen") > 0 || J.N(a, "locked_until") > NowUnix();
                await Admin("user", act(a, frozen ? "unfreeze" : "freeze"), frozen ? "Account entsperrt" : "Account gesperrt");
            };
            bDel.Click += async (s, e) =>
            {
                var a = sel(); if (a == null) return;
                if (!Dialogs.Confirm(FindForm(), "Löschen", "Panel-Account „" + name(a) + "“ endgültig löschen?")) return;
                await Admin("user", act(a, "delete"), "Account gelöscht");
            };
            return p;
        }

        static double NowUnix() { return (DateTime.UtcNow - new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc)).TotalSeconds; }

        /// <summary>Random start password (no look-alike characters).</summary>
        static string NewPassword()
        {
            const string chars = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789";
            var bytes = new byte[14];
            using (var rng = new System.Security.Cryptography.RNGCryptoServiceProvider()) rng.GetBytes(bytes);
            var sb = new StringBuilder();
            foreach (var b in bytes) sb.Append(chars[b % chars.Length]);
            return sb.ToString();
        }

        void ShowCredentials(string user, string pw)
        {
            var text = "RyvenPanel – Team-Zugang\r\nTeam-Server: https://" + getSsh().Host + ":" + port +
                       (string.IsNullOrEmpty(thumb) ? "" : "\r\nZertifikat (SHA-1): " + thumb) +
                       "\r\nBenutzername: " + user + "\r\nStartpasswort: " + pw + "\r\n(Beim ersten Login ein eigenes Passwort festlegen.)";
            try { Clipboard.SetText(text); } catch { }
            MessageBox.Show(FindForm(), text + "\r\n\r\nDiese Zugangsdaten wurden in die Zwischenablage kopiert. Gib sie nur privat weiter.\r\n" +
                            "Das Passwort wird nirgends im Klartext gespeichert – es kann später nur zurückgesetzt werden.",
                "Zugangsdaten", MessageBoxButtons.OK, MessageBoxIcon.Information);
        }

        /// <summary>Create (a == null) or edit an account: linked Minecraft player (LuckPerms rank) or a fixed rank.</summary>
        async Task EditAccount(object a)
        {
            const string fromLp = "– keiner (Rang aus LuckPerms) –";
            var ranks = new List<string> { fromLp };
            ranks.AddRange(roles.Where(kv => kv.Value.Count > 0).Select(kv => kv.Key).OrderBy(x => x));
            var groups = J.D(J.Get(state, "groups"));
            if (groups != null) ranks.AddRange(groups.Keys.Where(g => !ranks.Contains(g)).OrderBy(x => x));
            bool create = a == null;
            using (var d = new FormDialog(create ? "Team-Account anlegen" : "Account „" + J.S(a, "username") + "“ bearbeiten",
                "Rechte kommen entweder vom LuckPerms-Rang des verknüpften Minecraft-Spielers (Rangänderungen im Spiel wirken\n" +
                "automatisch) oder von einem festen Rang. Was jeder Rang darf, stellst du unter „Rechte“ ein."))
            {
                Field fName = null, fPw = null;
                if (create)
                {
                    fName = d.AddText("Benutzername (a–z, 0–9, _ . -)", "");
                    fPw = d.AddText("Startpasswort (muss beim ersten Login geändert werden)", NewPassword());
                }
                var fMc = d.AddText("Minecraft-Spieler verknüpfen (optional)", create ? "" : J.S(a, "mc_name") ?? "");
                var cRank = d.AddCombo("…oder fester Rang", ranks, create || string.IsNullOrEmpty(J.S(a, "role")) ? fromLp : J.S(a, "role"));
                d.AddNote("Ist ein Spieler eingetragen, gilt immer sein LuckPerms-Rang. Der Spieler muss einmal auf dem Server gewesen sein.", Theme.Muted);
                if (!d.Run(FindForm(), create ? "Anlegen" : "Speichern")) return;
                var mc = fMc.Box.Text.Trim();
                var role = cRank.SelectedItem as string == fromLp ? "" : (cRank.SelectedItem as string ?? "");
                if (mc.Length == 0 && role.Length == 0) { setStatus("Entweder einen Minecraft-Spieler verknüpfen oder einen festen Rang wählen.", Theme.Red); return; }
                if (role.Length > 0 && (!roles.ContainsKey(role) || roles[role].Count == 0))
                    setStatus("Hinweis: Rang „" + role + "“ hat noch keine Panel-Rechte – unter „Rechte“ freigeben.", Theme.Yellow);
                var data = new Dictionary<string, object> { { "action", create ? "create" : "update" }, { "mc_name", mc }, { "role", role } };
                if (create)
                {
                    var user = fName.Box.Text.Trim().ToLowerInvariant();
                    if (!Regex.IsMatch(user, @"^[a-z0-9_.\-]{3,32}$")) { setStatus("Benutzername: 3–32 Zeichen, nur a–z, 0–9, _ . -", Theme.Red); return; }
                    data["username"] = user;
                    data["password"] = fPw.Box.Text;
                    if (await Admin("user", data, "Account „" + user + "“ angelegt")) ShowCredentials(user, fPw.Box.Text);
                }
                else
                {
                    data["username"] = J.S(a, "username");
                    await Admin("user", data, "Account gespeichert");
                }
            }
        }

        Panel BuildRequests()
        {
            var p = new Panel { Dock = DockStyle.Fill, BackColor = Theme.Bg };
            var bOk = Ui.Btn("✓ Freigeben", Theme.Mix(Theme.Green, Theme.Bg, 0.35));
            var bNo = Ui.Btn("✕ Ablehnen", Theme.Mix(Theme.Red, Theme.Bg, 0.35));
            var bFw = Ui.Btn("IP in Firewall sperren", Theme.DarkRed).Tip("Diese IP komplett vom Server aussperren");
            var info = Theme.Lbl("Versucht sich ein Account von einer neuen IP anzumelden, wird der Login verweigert und landet hier.", Theme.F(8.75f), Theme.Muted, Theme.Bg);
            info.Margin = new Padding(4, 10, 0, 0);
            var bar = Ui.Bar(46, bOk, bNo, bFw, info);
            reqList = Ui.List(new[] { "Zeitpunkt", "Benutzer", "Neue IP", "Ort / Provider", "Bisher erlaubt" }, new[] { 130, 150, 130, 220, 200 });
            var c = new RoundPanel { Dock = DockStyle.Fill, Padding = new Padding(8, 10, 8, 8) };
            c.Controls.Add(reqList);
            p.Controls.Add(c);
            p.Controls.Add(bar);
            Func<string, Task> act = async a =>
            {
                var r = Ui.SelData<object>(reqList); if (r == null) return;
                await Admin("ip", new Dictionary<string, object> { { "username", J.S(r, "username") }, { "ip", J.S(r, "ip") }, { "action", a } }, a == "approve" ? "IP freigegeben" : "Anfrage abgelehnt");
            };
            bOk.Click += async (s, e) => await act("approve");
            bNo.Click += async (s, e) => await act("deny");
            bFw.Click += async (s, e) =>
            {
                var r = Ui.SelData<object>(reqList); if (r == null || GetFirewall == null) return;
                if (!Dialogs.Confirm(FindForm(), "Firewall", "IP " + J.S(r, "ip") + " komplett vom Server aussperren (auch Minecraft)?")) return;
                await act("deny");
                await GetFirewall().BlockIp(J.S(r, "ip"), "Panel-Login von " + J.S(r, "username") + " abgelehnt");
            };
            return p;
        }

        Panel BuildLog()
        {
            var p = new Panel { Dock = DockStyle.Fill, BackColor = Theme.Bg };
            logList = Ui.List(new[] { "Zeit", "Wer", "IP", "Aktion", "Details", "Ergebnis" }, new[] { 130, 120, 120, 170, 300, 110 });
            var c = new RoundPanel { Dock = DockStyle.Fill, Padding = new Padding(8, 10, 8, 8) };
            c.Controls.Add(logList);
            p.Controls.Add(c);
            return p;
        }

        Panel BuildSettings()
        {
            var p = new Panel { Dock = DockStyle.Fill, BackColor = Theme.Bg, AutoScroll = true };
            var card = new RoundPanel { Dock = DockStyle.Top, Height = 560, Padding = new Padding(22, 16, 22, 16) };
            int y = 16;
            Func<string, Label> cap = t => { var l = Theme.Lbl(t, Theme.F(8.25f, FontStyle.Bold), Theme.Muted, Color.Transparent); l.Location = new Point(22, y); card.Controls.Add(l); y += 22; return l; };
            cap("ADRESSE FÜR DEIN TEAM (in der App unter „Team-Server“ eintragen)");
            addrLbl = Theme.Lbl("", Theme.Mono, Theme.Cyan, Color.Transparent);
            addrLbl.Location = new Point(22, y); card.Controls.Add(addrLbl);
            var bCopy = new FlatBtn("Kopieren", Theme.CardHi) { Location = new Point(560, y - 6), Size = new Size(100, 30), Font = Theme.F(8.5f, FontStyle.Bold) };
            bCopy.Click += (s, e) => { try { Clipboard.SetText(addrLbl.Text); } catch { } };
            card.Controls.Add(bCopy);
            y += 50;
            cap("GESPERRTE KONSOLENBEFEHLE (gelten für alle Ränge ohne „Alle Konsolenbefehle“) – einer pro Zeile");
            blocked = new TextBox { Location = new Point(22, y), Size = new Size(430, 200), Multiline = true, ScrollBars = ScrollBars.Vertical, BackColor = Theme.Input, ForeColor = Theme.Text, BorderStyle = BorderStyle.None, Font = Theme.Mono };
            var bBlk = new FlatBtn("Speichern", Theme.Accent) { Location = new Point(462, y), Size = new Size(110, 34) };
            card.Controls.Add(blocked); card.Controls.Add(bBlk);
            y += 216;
            cap("DIENST");
            var bUpd = new FlatBtn("Aktualisieren / neu installieren", Theme.CardHi) { Location = new Point(22, y), Size = new Size(240, 34) };
            var bRestart = new FlatBtn("Neu starten", Theme.CardHi) { Location = new Point(270, y), Size = new Size(120, 34) };
            var bLog = new FlatBtn("Dienst-Log", Theme.CardHi) { Location = new Point(398, y), Size = new Size(120, 34) };
            card.Controls.AddRange(new Control[] { bUpd, bRestart, bLog });
            card.Height = y + 60;
            p.Controls.Add(card);

            bBlk.Click += async (s, e) =>
            {
                var list = blocked.Text.Split(new[] { '\r', '\n', ',', ' ' }, StringSplitOptions.RemoveEmptyEntries).Select(x => x.Trim().TrimStart('/')).Where(x => x.Length > 0).Distinct().ToArray();
                await Admin("config", new Dictionary<string, object> { { "blocked_commands", list } }, "Sperrliste gespeichert (" + list.Length + " Befehle)");
            };
            bUpd.Click += async (s, e) => await Install(port.ToString());
            bRestart.Click += async (s, e) => { var r = await getSsh().Run("systemctl restart ryvenpanel-gateway && sleep 1 && systemctl is-active ryvenpanel-gateway", 30000); setStatus("Dienst: " + r.Out.Trim() + r.Err.Trim(), r.Ok ? Theme.Green : Theme.Red); };
            bLog.Click += async (s, e) =>
            {
                var r = await getSsh().Run("journalctl -u ryvenpanel-gateway -n 40 --no-pager 2>&1", 30000);
                MessageBox.Show(FindForm(), r.Out.Length > 3000 ? r.Out.Substring(r.Out.Length - 3000) : r.Out, "Dienst-Log", MessageBoxButtons.OK, MessageBoxIcon.Information);
            };
            return p;
        }

        // ───────────────────────────── Data ─────────────────────────────
        static Dictionary<string, object> Parse(string output)
        {
            foreach (var line in (output ?? "").Split('\n').Reverse())
            {
                var l = line.Trim();
                if (!l.StartsWith("{")) continue;
                try { return J.D(J.Parse(l)); } catch { }
            }
            return null;
        }

        async Task Reload(bool showProgress)
        {
            if (busy) return;
            busy = true;
            if (showProgress) progress.Set(double.NaN);
            try
            {
                var r = await getSsh().Run(
                    "if [ ! -f /opt/ryvenpanel/gateway.py ]; then echo '@@NOTINSTALLED'; exit 0; fi\n" +
                    "echo \"@@ACTIVE $(systemctl is-active ryvenpanel-gateway)\"\n" +
                    "echo \"@@THUMB $(openssl x509 -noout -fingerprint -sha1 -in /etc/ryvenpanel/gw.crt 2>/dev/null | cut -d= -f2)\"\n" +
                    Py + " admin state", 45000);
                if (!r.Ok) { hSub.Text = "Fehler: " + r.Message; hSub.ForeColor = Theme.Red; return; }
                if (r.Out.Contains("@@NOTINSTALLED"))
                {
                    installed = false;
                    setup.Visible = true; body.Visible = false;
                    foreach (var t in tabs) t.Visible = false;
                    hSub.ForeColor = Theme.Muted;
                    hSub.Text = "Noch nicht eingerichtet – folge den drei Schritten.";
                    return;
                }
                installed = true;
                var active = Regex.Match(r.Out, @"@@ACTIVE (\S+)").Groups[1].Value;
                thumb = Regex.Match(r.Out, @"@@THUMB ([0-9A-F:]+)").Groups[1].Value;
                state = Parse(r.Out);
                if (state == null) { hSub.Text = "Antwort des Dienstes unlesbar: " + r.Message; hSub.ForeColor = Theme.Red; return; }
                setup.Visible = false; body.Visible = true;
                foreach (var t in tabs) t.Visible = true;
                port = (int)Math.Max(1, J.N(state, "port"));
                Render(active);
            }
            finally { busy = false; progress.Set(-1); }
            if (state != null && !updateTried && await EnsureCurrent(state)) await Reload(false);
        }

        void Render(string active)
        {
            var host = getSsh().Host;
            var accounts = J.L(J.Get(state, "users")).ToList();
            var requests = J.L(J.Get(state, "requests")).ToList();
            var sessions = J.L(J.Get(state, "sessions")).ToList();
            hSub.ForeColor = active == "active" ? Theme.Muted : Theme.Yellow;
            hSub.Text = (active == "active" ? "Dienst läuft" : "Dienst: " + active) + "  ·  https://" + host + ":" + port + "  ·  " + accounts.Count + " Accounts, " +
                        sessions.Count + " angemeldet" + (requests.Count > 0 ? "  ·  " + requests.Count + " IP-Anfrage(n) offen" : "") +
                        (accounts.Count == 0 ? "  ·  noch keine Accounts – unter „Accounts“ anlegen" : "");
            tabs[2].Text = requests.Count > 0 ? "IP-Freigaben (" + requests.Count + ")" : "IP-Freigaben";
            tabs[2].Width = Theme.Measure(tabs[2].Text, tabs[2].Font).Width + 30;
            if (PendingChanged != null) PendingChanged(requests.Count);
            addrLbl.Text = "https://" + host + ":" + port + (string.IsNullOrEmpty(thumb) ? "" : "      SHA-1 " + thumb);
            if (!blocked.Focused) blocked.Text = string.Join("\r\n", J.L(J.Get(state, "blocked_commands")).Select(Convert.ToString));

            // ranks
            roles = new Dictionary<string, List<string>>();
            var rd = J.D(J.Get(state, "roles"));
            if (rd != null) foreach (var kv in rd) roles[kv.Key] = J.L(kv.Value).Select(Convert.ToString).ToList();
            var groups = J.D(J.Get(state, "groups")) ?? new Dictionary<string, object>();
            var rows = new List<ListViewItem>();
            foreach (var g in groups.OrderByDescending(x => J.N(groups, x.Key)).ThenBy(x => x.Key))
            {
                List<string> ps;
                roles.TryGetValue(g.Key, out ps);
                int n = ps == null ? 0 : ps.Count;
                var row = Ui.Row(new RowTag { Key = g.Key, Dot = n == 0 ? Theme.Muted : ps.Contains("console.command.unrestricted") || ps.Contains("files.edit") ? Theme.Red : Theme.Green },
                    g.Key, ((int)J.N(groups, g.Key)).ToString(), n == 0 ? "kein Zugang" : n == PermCount ? "alles" : n + " Rechte");
                row.SubItems[2].ForeColor = n == 0 ? Theme.Muted : Theme.Green;
                rows.Add(row);
            }
            Ui.Fill(rankList, rows);
            if (permChecks.Count == 0) BuildPermChecks();
            if (selRank != null) ShowRank(selRank);
            if (!string.IsNullOrEmpty(J.S(state, "groups_error"))) setStatus("LuckPerms: " + J.S(state, "groups_error"), Theme.Yellow);

            // accounts
            var online = new HashSet<string>(sessions.Select(s => J.S(s, "username")));
            var arows = new List<ListViewItem>();
            var now = NowUnix();
            foreach (var a in accounts)
            {
                var u = J.S(a, "username");
                bool frozen = J.N(a, "frozen") > 0, locked = J.N(a, "locked_until") > now, on = online.Contains(u), fresh = J.N(a, "last_login") <= 0;
                var ips = string.Join(", ", J.L(J.Parse(J.S(a, "ips") ?? "[]")).Select(Convert.ToString));
                var src = !string.IsNullOrEmpty(J.S(a, "mc_name")) ? "Spieler " + J.S(a, "mc_name") : "fester Rang " + J.S(a, "role");
                var stat = frozen ? "gesperrt" : locked ? "vorübergehend gesperrt" : on ? "angemeldet" : fresh ? "noch nie angemeldet" : J.N(a, "must_change") > 0 ? "Passwort zurückgesetzt" : "abgemeldet";
                var row = Ui.Row(new RowTag { Key = u, Data = a, Dot = frozen || locked ? Theme.Red : on ? Theme.Green : Theme.Muted },
                    u, src, string.IsNullOrEmpty(J.S(a, "rank")) ? "–" : J.S(a, "rank"), ips.Length > 0 ? ips : "(beim ersten Login)", When(J.N(a, "last_login")), stat);
                row.SubItems[5].ForeColor = frozen || locked ? Theme.Red : on ? Theme.Green : Theme.Muted;
                arows.Add(row);
            }
            Ui.Fill(accList, arows);

            // requests
            var rrows = new List<ListViewItem>();
            foreach (var q in requests)
            {
                var acc = accounts.FirstOrDefault(a => J.S(a, "username") == J.S(q, "username"));
                var info = IpIntel.Get(J.S(q, "ip") ?? "");
                var row = Ui.Row(new RowTag { Key = J.S(q, "username") + J.S(q, "ip"), Data = q, Dot = Theme.Yellow },
                    When(J.N(q, "time")), J.S(q, "username"), J.S(q, "ip"),
                    info != null ? ((info.City ?? "") + " " + (info.CountryCode ?? "") + "  ·  " + (info.Org ?? info.Isp ?? "") + (info.Suspicious ? "  ⚠ " + info.Flags : "")).Trim() : "–",
                    acc != null ? string.Join(", ", J.L(J.Parse(J.S(acc, "ips") ?? "[]")).Select(Convert.ToString)) : "");
                rrows.Add(row);
            }
            Ui.Fill(reqList, rrows);

            // audit
            var lrows = new List<ListViewItem>();
            foreach (var l in J.L(J.Get(state, "audit")))
            {
                var res = J.S(l, "result") ?? "";
                bool bad = res != "ok";
                var row = Ui.Row(new RowTag { Key = J.S(l, "id"), Dot = bad ? Theme.Red : Theme.Green, Data = bad },
                    When(J.N(l, "time")), J.S(l, "name"), J.S(l, "ip"), J.S(l, "action"), J.S(l, "detail"), res);
                if (bad) row.SubItems[5].ForeColor = Theme.Red;
                lrows.Add(row);
            }
            Ui.Fill(logList, lrows);
        }

        int PermCount { get { return J.L(J.Get(state, "perms")).Count(); } }

        void BuildPermChecks()
        {
            permBox.Controls.Clear();
            permChecks.Clear();
            foreach (var p in J.L(J.Get(state, "perms")))
            {
                var arr = J.L(p).Select(Convert.ToString).ToArray();
                if (arr.Length < 2) continue;
                var cb = new CheckBox { Text = arr[1], UseMnemonic = false, AutoSize = true, ForeColor = Theme.Text, BackColor = Theme.Card, Font = Theme.F(10f), Margin = new Padding(0, 4, 0, 6), Enabled = false };
                if (arr[0] == "console.command.unrestricted" || arr[0] == "files.edit") cb.ForeColor = Theme.Mix(Theme.Text, Theme.Red, 0.35);
                cb.Tip(arr[0]);
                permChecks[arr[0]] = cb;
                permBox.Controls.Add(cb);
                DarkForm.ScaleLate(cb);
            }
        }

        void ShowRank(string rank)
        {
            selRank = rank;
            permTitle.Text = "Panel-Rechte für Rang „" + rank + "“";
            List<string> ps;
            roles.TryGetValue(rank, out ps);
            foreach (var kv in permChecks) { kv.Value.Enabled = true; kv.Value.Checked = ps != null && ps.Contains(kv.Key); }
        }

        static string When(double unix)
        {
            if (unix <= 0) return "–";
            var d = new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc).AddSeconds(unix).ToLocalTime();
            return d.ToString("dd.MM.yy HH:mm:ss");
        }

        /// <summary>Runs an owner command on the server. The payload goes through stdin (printf is a shell builtin), so passwords never show up in the process list.</summary>
        async Task<bool> Admin(string cmd, object data, string okText)
        {
            if (busy) return false;
            busy = true;
            progress.Set(double.NaN);
            bool ok = false;
            try
            {
                var b64 = Convert.ToBase64String(Encoding.UTF8.GetBytes(J.ToJson(data)));
                var r = await getSsh().Run("printf '%s' '" + b64 + "' | " + Py + " admin " + cmd + " -", 45000);
                var d = Parse(r.Out);
                if (!r.Ok || d == null || !J.B(d, "ok")) { setStatus("Fehler: " + (J.S(d, "error") ?? r.Message), Theme.Red); return false; }
                setStatus(okText, Theme.Green);
                ok = true;
            }
            finally { busy = false; progress.Set(-1); }
            await Reload(false);
            return ok;
        }

        // ───────────────────────────── Install ─────────────────────────────
        static string GatewaySource()
        {
            using (var s = Assembly.GetExecutingAssembly().GetManifestResourceStream("gateway.py"))
            using (var r = new StreamReader(s, Encoding.UTF8)) return r.ReadToEnd();
        }

        static string EcoSource()
        {
            using (var s = Assembly.GetExecutingAssembly().GetManifestResourceStream("eco.py"))
            using (var r = new StreamReader(s, Encoding.UTF8)) return r.ReadToEnd();
        }

        async Task Install(string portText)
        {
            int p;
            if (!int.TryParse(portText, out p) || p < 1024 || p > 65535 || p == 8443 || p == 25565) { setStatus("Ungültiger Port (1024–65535, nicht 8443/25565).", Theme.Red); return; }
            var token = craftyToken();
            if (string.IsNullOrEmpty(token)) { setStatus("Kein Crafty-Token – bitte als Owner über Crafty anmelden.", Theme.Red); return; }
            if (!Dialogs.Confirm(FindForm(), "Team-Zugang", "Der Team-Dienst wird auf dem Server installiert (Port " + p + ", Systemdienst „ryvenpanel-gateway“).\n\n" +
                    "Er nutzt deinen Crafty-Zugang stellvertretend – gibt ihn aber nie an das Team weiter.\n\nFortfahren?")) return;
            busy = true;
            progress.Set(double.NaN);
            setStatus("Team-Zugang wird installiert …", Theme.Muted);
            try
            {
                var unit = "[Unit]\nDescription=RyvenPanel Team-Gateway\nAfter=network-online.target docker.service\n\n[Service]\nExecStart=/usr/bin/python3 /opt/ryvenpanel/gateway.py\n" +
                           "Restart=always\nRestartSec=3\n\n[Install]\nWantedBy=multi-user.target\n";
                var script =
                    "set -e\nmkdir -p /opt/ryvenpanel /etc/ryvenpanel /var/lib/ryvenpanel\nchmod 700 /etc/ryvenpanel /var/lib/ryvenpanel\n" +
                    "printf '%s\\n' " + Ssh.B64(GatewaySource()) + " > /opt/ryvenpanel/gateway.py\n" +
                    "python3 -m py_compile /opt/ryvenpanel/gateway.py\n" +
                    "printf '%s\\n' " + Ssh.B64(EcoSource()) + " > /opt/ryvenpanel/eco.py\n" +
                    "[ -f /etc/ryvenpanel/gw.key ] || openssl req -x509 -newkey rsa:2048 -nodes -keyout /etc/ryvenpanel/gw.key -out /etc/ryvenpanel/gw.crt -days 3650 -subj '/CN=RyvenPanel Team' >/dev/null 2>&1\n" +
                    "chmod 600 /etc/ryvenpanel/gw.key\n" +
                    "LP=$(for d in /opt/crafty/docker/servers/*/; do [ -f \"$d/plugins/LuckPerms/config.yml\" ] && [ ! -f \"$d/velocity.toml\" ] && echo \"$d/plugins/LuckPerms/config.yml\"; done | head -1)\n" +
                    "[ -n \"$LP\" ] || { echo 'Kein Minecraft-Server mit LuckPerms gefunden.' >&2; exit 4; }\n" +
                    "RP_CRAFTY_TOKEN=" + Ssh.B64(token) + " RP_LP_CONFIG=\"$LP\" RP_PORT=" + p + " python3 /opt/ryvenpanel/gateway.py --init\n" +
                    "printf '%s' " + Ssh.B64(unit) + " > /etc/systemd/system/ryvenpanel-gateway.service\n" +
                    "systemctl daemon-reload\nsystemctl enable ryvenpanel-gateway >/dev/null 2>&1\nsystemctl restart ryvenpanel-gateway\n" +
                    "if [ -f /etc/ryvenpanel/firewall.conf ] && grep -qx 'mode strict' /etc/ryvenpanel/firewall.conf && ! grep -q '^allow tcp " + p + " ' /etc/ryvenpanel/firewall.conf; then\n" +
                    "  echo 'allow tcp " + p + " any # RyvenPanel Team-Zugang' >> /etc/ryvenpanel/firewall.conf; /usr/local/sbin/ryvenpanel-fw apply >/dev/null; echo FIREWALL_OPENED; fi\n" +
                    "sleep 2\nsystemctl is-active ryvenpanel-gateway\n";
                var r = await getSsh().Run(script, 120000);
                if (!r.Ok || !r.Out.Contains("active"))
                {
                    var log = await getSsh().Run("journalctl -u ryvenpanel-gateway -n 15 --no-pager 2>&1", 20000);
                    setStatus("Installation fehlgeschlagen: " + r.Message, Theme.Red);
                    if (setupInfo != null) { setupInfo.ForeColor = Theme.Red; setupInfo.Text = "Fehler: " + r.Message + "\n" + log.Out.Trim(); }
                    return;
                }
                setStatus("Team-Zugang läuft auf Port " + p + (r.Out.Contains("FIREWALL_OPENED") ? " (Firewall-Port geöffnet)" : "") + ".", Theme.Green);
            }
            finally { busy = false; progress.Set(-1); }
            await Reload(true);
        }
    }
}
