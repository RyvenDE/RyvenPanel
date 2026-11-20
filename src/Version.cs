using System.Reflection;

// The one place for the version number. Raise it for every release â€“ the panel compares it with the version
// published on the server and offers the update to everyone with an older one.
[assembly: AssemblyVersion(RyvenPanel.AppInfo.Version + ".0")]
[assembly: AssemblyFileVersion(RyvenPanel.AppInfo.Version + ".0")]
[assembly: AssemblyInformationalVersion(RyvenPanel.AppInfo.Version)]
[assembly: AssemblyProduct("RyvenPanel")]
[assembly: AssemblyCompany("RyvenMC")]
[assembly: AssemblyCopyright("RyvenMC")]

namespace RyvenPanel
{
    static class AppInfo
    {
        public const string Version = "1.6.1";
    }
}
