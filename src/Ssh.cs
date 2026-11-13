using System;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading.Tasks;

namespace RyvenPanel
{
    class SshResult
    {
        public int Exit;
        public string Out = "";
        public string Err = "";
        public bool Ok { get { return Exit == 0; } }

        public string Message
        {
            get
            {
                var e = (Err ?? "").Trim();
                if (e.Length == 0) e = (Out ?? "").Trim();
                return e.Length > 600 ? e.Substring(e.Length - 600) : e;
            }
        }
    }

    /// <summary>Runs shell scripts on the server through the Windows OpenSSH client (key auth only).</summary>
    class Ssh
    {
        public string Host, User, KeyPath;
        public int Port = 22;

        static readonly string SshExe = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System), "OpenSSH", "ssh.exe");
        static readonly string KeygenExe = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System), "OpenSSH", "ssh-keygen.exe");

        public static string KnownHosts
        {
            get
            {
                var dir = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "RyvenPanel");
                Directory.CreateDirectory(dir);
                return Path.Combine(dir, "known_hosts");
            }
        }

        public static string DefaultKey
        {
            get { return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".ssh", "ryvenpanel_ed25519"); }
        }

        public bool ClientAvailable { get { return File.Exists(SshExe); } }
        public bool KeyExists { get { return File.Exists(KeyPath) && File.Exists(KeyPath + ".pub"); } }
        public string PublicKey { get { return KeyExists ? File.ReadAllText(KeyPath + ".pub").Trim() : null; } }

        static readonly Regex SafeName = new Regex(@"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$");
        static readonly Regex SafeImage = new Regex(@"^[A-Za-z0-9][A-Za-z0-9._/:@-]{0,254}$");
        static readonly Regex SafePath = new Regex(@"^/[A-Za-z0-9._/-]{1,250}$");

        public static bool IsName(string s) { return s != null && SafeName.IsMatch(s); }
        public static bool IsImage(string s) { return s != null && SafeImage.IsMatch(s); }
        public static bool IsPath(string s) { return s != null && SafePath.IsMatch(s) && !s.Contains(".."); }

        /// <summary>Single-quotes a value for bash (callers validate names first; this is defense in depth).</summary>
        public static string Q(string s) { return "'" + (s ?? "").Replace("'", "'\\''") + "'"; }

        /// <summary>Embeds arbitrary text in a script without any quoting problems.</summary>
        public static string B64(string s) { return "\"$(echo " + Convert.ToBase64String(new UTF8Encoding(false).GetBytes(s ?? "")) + " | base64 -d)\""; }

        string BaseArgs()
        {
            return "-i " + Arg(KeyPath) + " -p " + Port +
                   " -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new" +
                   " -o UserKnownHostsFile=" + Arg(KnownHosts) +
                   " -o ConnectTimeout=10 -o ServerAliveInterval=15 ";
        }

        static string Arg(string s) { return "\"" + s.Replace("\"", "") + "\""; }

        public Task<SshResult> Run(string script, int timeoutMs = 60000)
        {
            return Task.Run(() =>
            {
                var res = new SshResult();
                if (!ClientAvailable) { res.Exit = -1; res.Err = "OpenSSH-Client fehlt (Windows-Feature „OpenSSH-Client“)."; return res; }
                if (!KeyExists) { res.Exit = -1; res.Err = "Kein SSH-Schlüssel eingerichtet."; return res; }
                int bom = PreambleLength;
                var remote = bom > 0 ? " \"head -c " + bom + " >/dev/null; exec bash -s\"" : " \"bash -s\"";
                var psi = new ProcessStartInfo(SshExe, BaseArgs() + "-T " + User + "@" + Host + remote)
                {
                    UseShellExecute = false,
                    CreateNoWindow = true,
                    RedirectStandardInput = true,
                    RedirectStandardOutput = true,
                    RedirectStandardError = true,
                    StandardOutputEncoding = Encoding.UTF8,
                    StandardErrorEncoding = Encoding.UTF8
                };
                try
                {
                    using (var p = Process.Start(psi))
                    {
                        NoPreamble(p);
                        var o = p.StandardOutput.ReadToEndAsync();
                        var e = p.StandardError.ReadToEndAsync();
                        var bytes = new UTF8Encoding(false).GetBytes("set -o pipefail\nexport LC_ALL=C.UTF-8\n" + script.Replace("\r\n", "\n") + "\n");
                        p.StandardInput.BaseStream.Write(bytes, 0, bytes.Length);
                        p.StandardInput.Close();
                        if (!p.WaitForExit(timeoutMs))
                        {
                            try { p.Kill(); } catch { }
                            res.Exit = -2;
                            res.Err = "Zeitüberschreitung nach " + (timeoutMs / 1000) + " s.";
                            return res;
                        }
                        p.WaitForExit();
                        res.Out = o.Result;
                        res.Err = e.Result;
                        res.Exit = p.ExitCode;
                        if (res.Exit == 255 && res.Err.Contains("Permission denied"))
                            res.Err = "SSH-Schlüssel wird vom Server abgelehnt. Ist er in /root/.ssh/authorized_keys eingetragen?\n\n" + res.Err;
                        if (res.Err.Contains("REMOTE HOST IDENTIFICATION HAS CHANGED"))
                            res.Err = "WARNUNG: Der Host-Schlüssel des Servers hat sich geändert! Verbindung abgebrochen.\n" +
                                      "Wenn du den Server neu installiert hast, entferne die Zeile in " + KnownHosts + ".";
                    }
                }
                catch (Exception ex)
                {
                    res.Exit = -1;
                    res.Err = ex.Message;
                }
                return res;
            });
        }

        /// <summary>
        /// Starts ssh with the script embedded in the command line (stdin/stdout stay free for raw data).
        /// tty=true allocates a remote pseudo terminal (interactive shell).
        /// </summary>
        public Process Start(string script, bool tty)
        {
            if (!ClientAvailable) throw new Exception("OpenSSH-Client fehlt (Windows-Feature „OpenSSH-Client“).");
            if (!KeyExists) throw new Exception("Kein SSH-Schlüssel eingerichtet (einmal unter „Docker“ einrichten).");
            // .NET writes the console encoding's preamble (BOM) to stdin at Process.Start – swallow exactly those bytes remotely.
            int bom = PreambleLength;
            if (bom > 0) script = (tty ? "RPS=$(stty -g); stty raw -echo; head -c " + bom + " >/dev/null; stty \"$RPS\"\n" : "head -c " + bom + " >/dev/null\n") + script;
            var b64 = Convert.ToBase64String(new UTF8Encoding(false).GetBytes(script.Replace("\r\n", "\n")));
            var remote = "\"bash -c \\\"$(echo " + b64 + " | base64 -d)\\\"\"";
            var psi = new ProcessStartInfo(SshExe, BaseArgs() + (tty ? "-tt " : "-T ") + User + "@" + Host + " " + remote)
            {
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardInput = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true
            };
            psi.EnvironmentVariables["TERM"] = "xterm-256color";
            var p = Process.Start(psi);
            NoPreamble(p);
            return p;
        }

        public static int PreambleLength
        {
            get { try { return Console.InputEncoding.GetPreamble().Length; } catch { return 0; } }
        }

        /// <summary>Prevents .NET from appending a UTF-8 BOM to stdin when the writer is flushed/closed.</summary>
        public static void NoPreamble(Process p)
        {
            try
            {
                var f = typeof(StreamWriter).GetField("haveWrittenPreamble", System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Instance);
                if (f != null) f.SetValue(p.StandardInput, true);
            }
            catch { }
        }

        public Task<SshResult> CreateKey()
        {
            return Task.Run(() =>
            {
                var res = new SshResult();
                try
                {
                    Directory.CreateDirectory(Path.GetDirectoryName(KeyPath));
                    var psi = new ProcessStartInfo(KeygenExe, "-t ed25519 -N \"\" -C \"RyvenPanel@" + Environment.MachineName + "\" -f " + Arg(KeyPath))
                    {
                        UseShellExecute = false,
                        CreateNoWindow = true,
                        RedirectStandardOutput = true,
                        RedirectStandardError = true,
                        RedirectStandardInput = true
                    };
                    using (var p = Process.Start(psi))
                    {
                        p.StandardInput.Close();
                        res.Out = p.StandardOutput.ReadToEnd();
                        res.Err = p.StandardError.ReadToEnd();
                        p.WaitForExit();
                        res.Exit = p.ExitCode;
                    }
                }
                catch (Exception ex) { res.Exit = -1; res.Err = ex.Message; }
                return res;
            });
        }

        /// <summary>Opens a visible console where the user types the root password once to install the public key.</summary>
        public void InstallKeyInteractive()
        {
            var pub = PublicKey;
            if (pub == null || pub.IndexOfAny(new[] { '\'', '"', '\r', '\n', '%', '^', '&' }) >= 0) return;
            var remote = "umask 077; mkdir -p ~/.ssh; grep -qxF '" + pub + "' ~/.ssh/authorized_keys 2>/dev/null || echo '" + pub +
                         "' >> ~/.ssh/authorized_keys; echo; echo FERTIG - Schluessel installiert. Fenster schliesst in 5 s.; sleep 5";
            var args = "/c \"\"" + SshExe + "\" -p " + Port + " -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=\"" + KnownHosts + "\" -t " +
                       User + "@" + Host + " \"" + remote + "\" || pause\"";
            Process.Start(new ProcessStartInfo("cmd.exe", args) { UseShellExecute = true });
        }
    }
}
