using System;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Windows.Forms;

// Windows 10/11 include the .NET Framework CLR used by this small launcher.
// Electron's Windows compatibility switch must be present before native startup.
internal static class Launcher
{
    internal static string Quote(string value)
    {
        var result = new StringBuilder("\"");
        int slashes = 0;
        foreach (char character in value)
        {
            if (character == '\\') { slashes++; continue; }
            if (character == '"') result.Append('\\', slashes * 2 + 1);
            else result.Append('\\', slashes);
            slashes = 0;
            result.Append(character);
        }
        result.Append('\\', slashes * 2);
        result.Append('"');
        return result.ToString();
    }

    [STAThread]
    private static int Main(string[] arguments)
    {
        string directory = AppDomain.CurrentDomain.BaseDirectory;
        string executable = Path.Combine(directory, "PepsicodeDesktop.exe");
        try
        {
            if (!File.Exists(executable)) throw new FileNotFoundException("请完整解压 ZIP，再运行 Pepsicode.exe。", executable);
            var forwarded = new StringBuilder("--no-sandbox");
            foreach (string argument in arguments) forwarded.Append(" ").Append(Quote(argument));
            var start = new ProcessStartInfo(executable, forwarded.ToString());
            start.WorkingDirectory = directory;
            start.UseShellExecute = false;
            start.CreateNoWindow = true;
            using (Process process = Process.Start(start))
            {
                process.WaitForExit();
                return process.ExitCode;
            }
        }
        catch (Exception error)
        {
            MessageBox.Show(error.Message, "Pepsicode 启动失败", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }
}
