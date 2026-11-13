
using System;
using System.Collections.Generic;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Imaging;
using System.Globalization;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.RegularExpressions;
using System.Windows.Forms;

namespace RelayGen
{
   
    public static class BatBuilder
    {
        public const string TplResourceSuffix = "template.dat";

        public class Result
        {
            public bool Ok;
            public string Error;
            public string RelayUrl;
            public string OutPath;
            public long Size;
            public string Transport = TransportOss;
        }

        public static string Normalize(string hostInput, string portInput,
                                       out string error, out string hostOnly, out int portNum)
        {
            error = null;
            hostOnly = null;
            portNum = 0;

            string host = (hostInput ?? "").Trim();
            if (host.Length == 0)
            {
                error = "请填写服务器地址。";
                return null;
            }

            host = Regex.Replace(host, @"^\s*https?://", "", RegexOptions.IgnoreCase);
            int slash = host.IndexOf('/');
            if (slash >= 0) host = host.Substring(0, slash);
            host = host.Trim();

            string embeddedPort = "";
            int colon = host.LastIndexOf(':');
            if (colon > 0)
            {
                string tail = host.Substring(colon + 1);
                if (Regex.IsMatch(tail, @"^\d{1,5}$"))
                {
                    embeddedPort = tail;
                    host = host.Substring(0, colon);
                }
            }

            if (!Regex.IsMatch(host, @"^[0-9A-Za-z.\-]+$"))
            {
                error = "服务器地址只能包含字母、数字、点、短横线。只填 IP 或域名，不要带路径。";
                return null;
            }

            string port = (embeddedPort.Length > 0 ? embeddedPort : (portInput ?? "").Trim());
            if (port.Length == 0) port = "8080";
            int pn;
            if (!int.TryParse(port, NumberStyles.Integer, CultureInfo.InvariantCulture, out pn)
                || pn < 1 || pn > 65535)
            {
                error = "端口必须是 1-65535 之间的数字。";
                return null;
            }

            hostOnly = host;
            portNum = pn;
            return "http://" + host + ":" + pn;
        }

        public static bool ValidateKey(string key, out string error)
        {
            error = null;
            if (string.IsNullOrEmpty(key) || key.Trim().Length == 0)
            {
                error = "请填写设备密钥。在服务器的部署输出里能找到。";
                return false;
            }
            key = key.Trim();
            if (!Regex.IsMatch(key, @"^[0-9A-Za-z_\-]{8,128}$"))
            {
                error = "设备密钥格式不对，应为 8-128 位字母、数字、下划线或短横线。";
                return false;
            }
            return true;
        }

        public const string TransportOss = "oss";
        public const string TransportAuto = "auto";
        public const string TransportIpv6 = "ipv6";

        public static bool ValidateTransport(string transport, out string normalized)
        {
            normalized = TransportOss;
            string t = (transport ?? "").Trim().ToLowerInvariant();
            if (t.Length == 0) return true;  
            if (t == TransportOss || t == TransportAuto || t == TransportIpv6)
            {
                normalized = t;
                return true;
            }
            return false;
        }

        public static string TransportLabel(string transport)
        {
            switch ((transport ?? "").Trim().ToLowerInvariant())
            {
                case TransportAuto: return "IPv6 优先，失败回落 OSS";
                case TransportIpv6: return "仅 IPv6";
                default: return "仅 OSS";
            }
        }

        public static string LoadTemplate()
        {
            try
            {
                string dir = Path.GetDirectoryName(Application.ExecutablePath);
                string ext = Path.Combine(dir, TplResourceSuffix);
                if (File.Exists(ext))
                    return File.ReadAllText(ext, Encoding.UTF8);
            }
            catch { }

            try
            {
                var asm = System.Reflection.Assembly.GetExecutingAssembly();
                foreach (string name in asm.GetManifestResourceNames())
                {
                    if (name.EndsWith(TplResourceSuffix, StringComparison.OrdinalIgnoreCase))
                    {
                        using (var s = asm.GetManifestResourceStream(name))
                        using (var r = new StreamReader(s, Encoding.UTF8))
                            return r.ReadToEnd();
                    }
                }
            }
            catch { }
            return null;
        }

        public static string DefaultOutDir()
        {
            try
            {
                string dir = Path.GetDirectoryName(Application.ExecutablePath);
                string probe = Path.Combine(dir, ".wt_" + Guid.NewGuid().ToString("N") + ".tmp");
                File.WriteAllText(probe, "x");
                File.Delete(probe);
                return dir;
            }
            catch
            {
                return Environment.GetFolderPath(Environment.SpecialFolder.Desktop);
            }
        }

        public static Result Build(string host, string port, string key,
                                   bool autostart, string outPath)
        {
            return Build(host, port, key, autostart, TransportOss, outPath);
        }

        public static Result Build(string host, string port, string key,
                                   bool autostart, string transport, string outPath)
        {
            var res = new Result();

            string err, hostOnly;
            int portNum;
            string url = Normalize(host, port, out err, out hostOnly, out portNum);
            if (url == null) { res.Error = err; return res; }

            if (!ValidateKey(key, out err)) { res.Error = err; return res; }
            key = key.Trim();

            string xport;
            if (!ValidateTransport(transport, out xport))
            {
                res.Error = "传输方式只能是 oss / auto / ipv6。";
                return res;
            }

            res.RelayUrl = url;
            res.Transport = xport;

            string tpl = LoadTemplate();
            if (tpl == null)
            {
                res.Error = "找不到模板数据，EXE 可能损坏。请重新下载。";
                return res;
            }
            if (tpl.IndexOf("#PSSTART") < 0)
            {
                res.Error = "模板缺少 #PSSTART 标记，EXE 已损坏。";
                return res;
            }

            string bat = tpl
                .Replace("__RELAY_URL__", url)
                .Replace("__DEVICE_KEY__", key)
                .Replace("__AUTOSTART__", autostart ? "1" : "0")
                .Replace("__TRANSPORT__", xport);

            bat = bat.Replace("\r\n", "\n").Replace("\r", "\n").Replace("\n", "\r\n");

            int ps = bat.IndexOf("#PSSTART");
            for (int i = 0; i < ps; i++)
            {
                if (bat[i] > 127)
                {
                    res.Error = "模板头部含非 ASCII 字符（位置 " + i + "），生成会失败。";
                    return res;
                }
            }

            if (string.IsNullOrEmpty(outPath))
                outPath = Path.Combine(DefaultOutDir(), "传输助手.bat");

            string parent = Path.GetDirectoryName(outPath);
            if (!string.IsNullOrEmpty(parent) && !Directory.Exists(parent))
                Directory.CreateDirectory(parent);

            File.WriteAllText(outPath, bat, new UTF8Encoding(false));

            res.Ok = true;
            res.OutPath = outPath;
            res.Size = new FileInfo(outPath).Length;
            return res;
        }
    }

    public static class Theme
    {
       
        public static readonly Color Bg = Color.FromArgb(0xF5, 0xF7, 0xFB);
        public static readonly Color BgDeep = Color.FromArgb(0xEC, 0xF1, 0xF9);
        public static readonly Color Card = Color.White;
        public static readonly Color CardAlt = Color.FromArgb(0xFA, 0xFB, 0xFE);
        public static readonly Color Line = Color.FromArgb(0xE2, 0xE7, 0xF0);
        public static readonly Color LineSoft = Color.FromArgb(0xEE, 0xF1, 0xF7);

        public static readonly Color Text = Color.FromArgb(0x1B, 0x1F, 0x27);
        public static readonly Color TextSub = Color.FromArgb(0x62, 0x6B, 0x7A);
        public static readonly Color TextHint = Color.FromArgb(0x9A, 0xA3, 0xB2);

        public static readonly Color Accent = Color.FromArgb(0x2F, 0x6B, 0xF0);
        public static readonly Color Accent2 = Color.FromArgb(0x7A, 0x5A, 0xF8);
        public static readonly Color AccentSoft = Color.FromArgb(0xEC, 0xF2, 0xFF);

        public static readonly Color Ok = Color.FromArgb(0x1A, 0x9E, 0x5B);
        public static readonly Color OkSoft = Color.FromArgb(0xE7, 0xF7, 0xEE);
        public static readonly Color Warn = Color.FromArgb(0xC8, 0x82, 0x1A);
        public static readonly Color Err = Color.FromArgb(0xD1, 0x3B, 0x3B);
        public static readonly Color ErrSoft = Color.FromArgb(0xFD, 0xEC, 0xEC);

        public static readonly Color LogBg = Color.FromArgb(0x1E, 0x22, 0x2B);

        public const string Engineer = "工程师：梦宛（EchoEon5）· QQ：742965044";

        public static readonly Font F9 = new Font("Microsoft YaHei UI", 9F);
        public static readonly Font F10 = new Font("Microsoft YaHei UI", 10F);
        public static readonly Font F105 = new Font("Microsoft YaHei UI", 10.5F);
        public static readonly Font F12 = new Font("Microsoft YaHei UI", 12F);
        public static readonly Font FB9 = new Font("Microsoft YaHei UI", 9F, FontStyle.Bold);
        public static readonly Font FB11 = new Font("Microsoft YaHei UI", 11F, FontStyle.Bold);
        public static readonly Font FB16 = new Font("Microsoft YaHei UI", 16F, FontStyle.Bold);
        public static readonly Font F8 = new Font("Microsoft YaHei UI", 8F);
        public static readonly Font Mono95 = new Font("Consolas", 9.5F);

        public static GraphicsPath RoundRect(Rectangle r, int radius)
        {
            var p = new GraphicsPath();
            if (radius <= 0)
            {
                p.AddRectangle(r);
                return p;
            }
            int d = radius * 2;
            if (d > r.Width) d = r.Width;
            if (d > r.Height) d = r.Height;
            p.AddArc(r.X, r.Y, d, d, 180, 90);
            p.AddArc(r.Right - d, r.Y, d, d, 270, 90);
            p.AddArc(r.Right - d, r.Bottom - d, d, d, 0, 90);
            p.AddArc(r.X, r.Bottom - d, d, d, 90, 90);
            p.CloseFigure();
            return p;
        }

        public static void FillRound(Graphics g, Rectangle r, int radius, Color color)
        {
            using (var p = RoundRect(r, radius))
            using (var b = new SolidBrush(color))
                g.FillPath(b, p);
        }

        public static void StrokeRound(Graphics g, Rectangle r, int radius, Color color, float width)
        {
            using (var p = RoundRect(r, radius))
            using (var pen = new Pen(color, width))
                g.DrawPath(pen, p);
        }

        public static LinearGradientBrush Gradient(Rectangle r)
        {
            return new LinearGradientBrush(
                new Point(r.Left, r.Top), new Point(r.Right, r.Top),
                Accent, Accent2);
        }

        public static void HiQuality(Graphics g)
        {
            g.SmoothingMode = SmoothingMode.AntiAlias;
            g.TextRenderingHint = System.Drawing.Text.TextRenderingHint.ClearTypeGridFit;
            g.PixelOffsetMode = PixelOffsetMode.HighQuality;
        }
    }

    public static class Icons
    {
        public static void Server(Graphics g, Rectangle r, Color c)
        {
            using (var pen = new Pen(c, 1.6f))
            {
                int h = Math.Max(5, r.Height / 3);
                var top = new Rectangle(r.X, r.Y + 1, r.Width, h);
                var bot = new Rectangle(r.X, r.Bottom - h - 1, r.Width, h);
                using (var p1 = Theme.RoundRect(top, 2)) g.DrawPath(pen, p1);
                using (var p2 = Theme.RoundRect(bot, 2)) g.DrawPath(pen, p2);
                using (var b = new SolidBrush(c))
                {
                    g.FillEllipse(b, r.X + 3, top.Y + h / 2 - 1, 2, 2);
                    g.FillEllipse(b, r.X + 3, bot.Y + h / 2 - 1, 2, 2);
                }
            }
        }

        public static void Home(Graphics g, Rectangle r, Color c)
        {
            using (var pen = new Pen(c, 1.6f))
            {
                var pts = new Point[] {
                    new Point(r.X + r.Width / 2, r.Y + 1),
                    new Point(r.Right - 2, r.Y + r.Height / 2),
                    new Point(r.X + 2, r.Y + r.Height / 2)
                };
                g.DrawPolygon(pen, pts);
                g.DrawRectangle(pen, r.X + 4, r.Y + r.Height / 2, r.Width - 8, r.Height / 2 - 2);
            }
        }

        public static void Gear(Graphics g, Rectangle r, Color c)
        {
            using (var pen = new Pen(c, 1.5f))
            {
                float cx = r.X + r.Width / 2f, cy = r.Y + r.Height / 2f;
                float rad = r.Width / 2f - 3.5f;
                g.DrawEllipse(pen, cx - rad, cy - rad, rad * 2, rad * 2);
                g.DrawEllipse(pen, cx - 2.5f, cy - 2.5f, 5, 5);
                for (int i = 0; i < 6; i++)
                {
                    double a = Math.PI * 2 * i / 6 + Math.PI / 6;
                    float x1 = cx + (float)Math.Cos(a) * (rad + 0.5f);
                    float y1 = cy + (float)Math.Sin(a) * (rad + 0.5f);
                    float x2 = cx + (float)Math.Cos(a) * (rad + 2.8f);
                    float y2 = cy + (float)Math.Sin(a) * (rad + 2.8f);
                    g.DrawLine(pen, x1, y1, x2, y2);
                }
            }
        }

        public static void Book(Graphics g, Rectangle r, Color c)
        {
            using (var pen = new Pen(c, 1.6f))
            {
                var box = new Rectangle(r.X + 1, r.Y + 1, r.Width - 2, r.Height - 2);
                using (var p = Theme.RoundRect(box, 2)) g.DrawPath(pen, p);
                g.DrawLine(pen, r.X + 4, r.Y + r.Height / 2, r.Right - 4, r.Y + r.Height / 2);
            }
        }

        public static void Info(Graphics g, Rectangle r, Color c)
        {
            using (var pen = new Pen(c, 1.5f))
            {
                g.DrawEllipse(pen, r.X + 1, r.Y + 1, r.Width - 2, r.Height - 2);
                using (var b = new SolidBrush(c))
                {
                    g.FillEllipse(b, r.X + r.Width / 2 - 1, r.Y + 4, 2, 2);
                    g.FillRectangle(b, r.X + r.Width / 2 - 1, r.Y + 8, 2, r.Height - 12);
                }
            }
        }

        public static void Eye(Graphics g, Rectangle r, Color c, bool closed)
        {
            using (var pen = new Pen(c, 1.5f))
            {
                int cx = r.X + r.Width / 2, cy = r.Y + r.Height / 2;
                int w = r.Width - 4, h = (r.Height - 6) / 2 + 2;
                using (var p = new GraphicsPath())
                {
                    p.AddArc(cx - w / 2, cy - h / 2, w, h, 200, 140);
                    p.AddArc(cx - w / 2, cy - h / 2, w, h, 20, 140);
                    g.DrawPath(pen, p);
                }
                g.DrawEllipse(pen, cx - 2, cy - 2, 4, 4);
                if (closed) g.DrawLine(pen, r.X + 3, r.Bottom - 3, r.Right - 3, r.Y + 3);
            }
        }

        public static void Check(Graphics g, Rectangle r, Color c, float width)
        {
            using (var pen = new Pen(c, width))
            {
                pen.StartCap = LineCap.Round;
                pen.EndCap = LineCap.Round;
                pen.LineJoin = LineJoin.Round;
                g.DrawLines(pen, new Point[] {
                    new Point(r.X + (int)(r.Width * 0.18f), (int)(r.Y + r.Height * 0.52f)),
                    new Point(r.X + (int)(r.Width * 0.42f), (int)(r.Y + r.Height * 0.76f)),
                    new Point(r.X + (int)(r.Width * 0.84f), (int)(r.Y + r.Height * 0.24f))
                });
            }
        }

        public static void Copy(Graphics g, Rectangle r, Color c)
        {
            using (var pen = new Pen(c, 1.5f))
            {
                var front = new Rectangle(r.X + 3, r.Y + 3, r.Width - 6, r.Height - 6);
                using (var p = Theme.RoundRect(front, 2)) g.DrawPath(pen, p);
                g.DrawLines(pen, new Point[] {
                    new Point(r.X + 1, r.Bottom - 4),
                    new Point(r.X + 1, r.Y + 2),
                    new Point(r.Right - 4, r.Y + 2)
                });
            }
        }

        public static void Folder(Graphics g, Rectangle r, Color c)
        {
            using (var pen = new Pen(c, 1.5f))
            {
                var body = new Rectangle(r.X + 1, r.Y + 4, r.Width - 2, r.Height - 6);
                using (var p = Theme.RoundRect(body, 2)) g.DrawPath(pen, p);
                g.DrawLine(pen, r.X + 2, r.Y + 4, r.X + 6, r.Y + 1);
                g.DrawLine(pen, r.X + 6, r.Y + 1, r.X + 10, r.Y + 4);
            }
        }

        public static void Close(Graphics g, Rectangle r, Color c)
        {
            using (var pen = new Pen(c, 1.5f))
            {
                int m = 3;
                g.DrawLine(pen, r.X + m, r.Y + m, r.Right - m, r.Bottom - m);
                g.DrawLine(pen, r.Right - m, r.Y + m, r.X + m, r.Bottom - m);
            }
        }
    }

    public class Card : Panel
    {
        public int Radius = 14;
        public string Title = "";
        public bool ShowShadow = true;

        public Card()
        {
            SetStyle(ControlStyles.OptimizedDoubleBuffer | ControlStyles.AllPaintingInWmPaint |
                     ControlStyles.UserPaint | ControlStyles.ResizeRedraw |
                         ControlStyles.SupportsTransparentBackColor, true);
            BackColor = Color.Transparent;
        }

        protected override void OnPaint(PaintEventArgs e)
        {
            var g = e.Graphics;
            Theme.HiQuality(g);

            if (ShowShadow)
            {
                for (int i = 3; i >= 1; i--)
                {
                    var sr = new Rectangle(0, i, Width - 1, Height - 1);
                    if (sr.Height <= 0) continue;
                    using (var p = Theme.RoundRect(sr, Radius))
                    using (var b = new SolidBrush(Color.FromArgb(8, 0x6A, 0x82, 0xB4)))
                        g.FillPath(b, p);
                }
            }

            var rr = new Rectangle(0, 0, Width - 1, Height - 2);
            using (var p = Theme.RoundRect(rr, Radius))
            using (var b = new SolidBrush(Theme.Card))
                g.FillPath(b, p);
            using (var p = Theme.RoundRect(rr, Radius))
            using (var pen = new Pen(Theme.Line, 1f))
                g.DrawPath(pen, p);

            if (Title.Length > 0)
            {
                using (var b = new SolidBrush(Theme.TextSub))
                    g.DrawString(Title, Theme.FB9, b, 18, 14);
            }
        }
    }

    public class InputBox : Panel
    {
        private readonly TextBox _box;
        private readonly Timer _anim;
        private float _focus;         
        private bool _hot;            
        private bool _reveal;         

        public bool UseIcon;
        public bool IsPassword;
        public int Radius = 10;

        public InputBox()
        {
            SetStyle(ControlStyles.OptimizedDoubleBuffer | ControlStyles.AllPaintingInWmPaint |
                     ControlStyles.UserPaint | ControlStyles.ResizeRedraw |
                         ControlStyles.SupportsTransparentBackColor, true);
            BackColor = Color.Transparent;

            _box = new TextBox();
            _box.BorderStyle = BorderStyle.None;
            _box.Font = Theme.F105;
            _box.BackColor = Theme.Card;
            _box.ForeColor = Theme.Text;
            _box.Enter += delegate { Fade(1f); };
            _box.Leave += delegate { Fade(0f); };
            _box.TextChanged += delegate { if (TextChanged2 != null) TextChanged2(this, EventArgs.Empty); };
            _box.KeyDown += delegate(object s, KeyEventArgs k)
            {
                if (k.KeyCode == Keys.Enter && Submit != null) Submit(this, EventArgs.Empty);
            };
            Controls.Add(_box);

            Height = 44;
            LayoutBox();

            _anim = new Timer();
            _anim.Interval = 16;
            _anim.Tick += delegate
            {
                float target = _focusOn ? 1f : 0f;
                float d = target - _focus;
                if (Math.Abs(d) < 0.08f) { _focus = target; _anim.Stop(); }
                else _focus += d * 0.35f;
                Invalidate();
            };

            MouseEnter += delegate { _hot = true; Invalidate(); };
            MouseLeave += delegate { _hot = false; Invalidate(); };
            Click += delegate { _box.Focus(); };
            foreach (Control c in Controls) c.Click += delegate { _box.Focus(); };
        }

        public event EventHandler TextChanged2;
        public event EventHandler Submit;
        private bool _focusOn;

        public TextBox Inner { get { return _box; } }

        public string ValueText
        {
            get { return _box.Text; }
            set { _box.Text = value; }
        }

        public string Placeholder = "";

        private void Fade(float to)
        {
            _focusOn = to > 0.5f;
            _anim.Start();
        }

        protected override void OnResize(EventArgs e)
        {
            base.OnResize(e);
            LayoutBox();
        }

        private void LayoutBox()
        {
           
            if (_box == null) return;
            int left = UseIcon ? 46 : 14;
            int right = IsPassword ? 40 : 14;
            _box.Left = left;
            _box.Width = Math.Max(20, Width - left - right);
            _box.Top = (Height - _box.PreferredHeight) / 2 + 1;
            _box.UseSystemPasswordChar = IsPassword && !_reveal;
        }

        protected override void OnPaint(PaintEventArgs e)
        {
            var g = e.Graphics;
            Theme.HiQuality(g);

            var rr = new Rectangle(0, 0, Width - 1, Height - 1);
            using (var p = Theme.RoundRect(rr, Radius))
            using (var b = new SolidBrush(Theme.Card))
                g.FillPath(b, p);

            Color border = _focus <= 0f
                ? (_hot ? Color.FromArgb(0xC8, 0xD2, 0xE4) : Theme.Line)
                : Lerp(Color.FromArgb(0xC8, 0xD2, 0xE4), Theme.Accent, _focus);
            float w = 1f + _focus * 0.6f;

            using (var p = Theme.RoundRect(rr, Radius))
            using (var pen = new Pen(border, w))
                g.DrawPath(pen, p);

            if (_focus > 0.05f)
            {
                var glow = new Rectangle(-2, -2, Width + 3, Height + 3);
                using (var p = Theme.RoundRect(glow, Radius + 2))
                using (var pen = new Pen(Color.FromArgb((int)(46 * _focus), Theme.Accent), 2.4f))
                    g.DrawPath(pen, p);
            }

            if (UseIcon)
            {
                var ir = new Rectangle(14, Height / 2 - 9, 18, 18);
                Icons.Server(g, ir, _focus > 0.5f ? Theme.Accent : Theme.TextHint);
            }

            if (IsPassword)
            {
                var er = new Rectangle(Width - 32, Height / 2 - 9, 18, 18);
                Icons.Eye(g, er, _hot ? Theme.TextSub : Theme.TextHint, !_reveal);
            }

            if (_box.Text.Length == 0 && Placeholder.Length > 0 && !_box.Focused)
            {
                using (var b = new SolidBrush(Theme.TextHint))
                    g.DrawString(Placeholder, Theme.F10, b, UseIcon ? 46 : 14, Height / 2 - 10);
            }
        }

        protected override void OnMouseDown(MouseEventArgs e)
        {
            base.OnMouseDown(e);
            if (IsPassword && e.X >= Width - 40)
            {
                _reveal = !_reveal;
                LayoutBox();
                _box.Focus();
                Invalidate();
                return;
            }
            _box.Focus();
        }

        private static Color Lerp(Color a, Color b, float t)
        {
            if (t < 0) t = 0; if (t > 1) t = 1;
            return Color.FromArgb(
                (int)(a.R + (b.R - a.R) * t),
                (int)(a.G + (b.G - a.G) * t),
                (int)(a.B + (b.B - a.B) * t));
        }
    }

    public class ToggleSwitch : Control
    {
        private readonly Timer _anim;
        private float _pos;         
        private bool _on;

        public event EventHandler Changed;

        public ToggleSwitch()
        {
            SetStyle(ControlStyles.OptimizedDoubleBuffer | ControlStyles.AllPaintingInWmPaint |
                     ControlStyles.UserPaint | ControlStyles.ResizeRedraw |
                         ControlStyles.SupportsTransparentBackColor, true);
            Size = new Size(46, 26);
            Cursor = Cursors.Hand;

            _anim = new Timer();
            _anim.Interval = 16;
            _anim.Tick += delegate
            {
                float target = _on ? 1f : 0f;
                float d = target - _pos;
                if (Math.Abs(d) < 0.05f) { _pos = target; _anim.Stop(); }
                else _pos += d * 0.32f;
                Invalidate();
            };
        }

        public bool Checked
        {
            get { return _on; }
            set
            {
                if (_on == value) return;
                _on = value;
                _anim.Start();
                if (Changed != null) Changed(this, EventArgs.Empty);
            }
        }

        protected override void OnClick(EventArgs e)
        {
            base.OnClick(e);
            Checked = !_on;
        }

        protected override void OnPaint(PaintEventArgs e)
        {
            var g = e.Graphics;
            Theme.HiQuality(g);

            var track = new Rectangle(0, 0, Width - 1, Height - 1);
            int rad = Height / 2;

            using (var p = Theme.RoundRect(track, rad))
            {
                if (_pos <= 0.01f)
                {
                    using (var b = new SolidBrush(Color.FromArgb(0xD5, 0xDC, 0xE8)))
                        g.FillPath(b, p);
                }
                else
                {
                   
                    Color c = _pos >= 0.99f
                        ? Theme.Accent
                        : Color.FromArgb(
                            (int)(0xD5 + (Theme.Accent.R - 0xD5) * _pos),
                            (int)(0xDC + (Theme.Accent.G - 0xDC) * _pos),
                            (int)(0xE8 + (Theme.Accent.B - 0xE8) * _pos));
                    using (var b = new SolidBrush(c))
                        g.FillPath(b, p);
                }
            }

            int knob = Height - 6;
            int x = (int)(3 + (Width - knob - 6) * _pos);
            using (var b = new SolidBrush(Color.White))
                g.FillEllipse(b, x, 3, knob, knob);
            using (var pen = new Pen(Color.FromArgb(24, 0, 0, 0), 1f))
                g.DrawEllipse(pen, x, 3, knob, knob);
        }
    }

    public class PrimaryButton : Control
    {
        private bool _hot, _down;
        public int Radius = 12;
        public string Glyph = "";

        public PrimaryButton()
        {
            SetStyle(ControlStyles.OptimizedDoubleBuffer | ControlStyles.AllPaintingInWmPaint |
                     ControlStyles.UserPaint | ControlStyles.ResizeRedraw |
                         ControlStyles.SupportsTransparentBackColor, true);
            Size = new Size(200, 46);
            Cursor = Cursors.Hand;
            MouseEnter += delegate { _hot = true; Invalidate(); };
            MouseLeave += delegate { _hot = false; _down = false; Invalidate(); };
            MouseDown += delegate { _down = true; Invalidate(); };
            MouseUp += delegate { _down = false; Invalidate(); };
        }

        public bool Enabled2 = true;

        protected override void OnPaint(PaintEventArgs e)
        {
            var g = e.Graphics;
            Theme.HiQuality(g);

            int inset = _down ? 2 : 0;
            var rr = new Rectangle(inset, inset, Width - 1 - inset * 2, Height - 2 - inset * 2 - (_down ? 0 : 0));
            if (rr.Width <= 2 || rr.Height <= 2) return;

            using (var p = Theme.RoundRect(rr, Radius))
            {
                if (!Enabled2)
                {
                    using (var b = new SolidBrush(Color.FromArgb(0xC7, 0xCE, 0xDB)))
                        g.FillPath(b, p);
                }
                else
                {
                    var gradRect = new Rectangle(0, rr.Y, Width, rr.Height);
                    using (var b = Theme.Gradient(gradRect))
                        g.FillPath(b, p);
                    if (_hot)
                    {
                        using (var b2 = new SolidBrush(Color.FromArgb(_down ? 0 : 18, 255, 255, 255)))
                            g.FillPath(b2, p);
                    }
                }
            }

            using (var p = Theme.RoundRect(new Rectangle(rr.X + 1, rr.Y + 1, rr.Width - 2,
                       Math.Max(2, rr.Height / 2)), Radius))
            using (var b = new SolidBrush(Color.FromArgb(26, 255, 255, 255)))
                g.FillPath(b, p);

            string t = Text ?? "";
            var sz = g.MeasureString(t, Theme.FB11);
            float tx = (Width - sz.Width) / 2f;
            float ty = (Height - sz.Height) / 2f;
            using (var b = new SolidBrush(Color.White))
                g.DrawString(t, Theme.FB11, b, tx, ty);
        }
    }

    public class GhostButton : Control
    {
        private bool _hot, _down;
        public int Radius = 12;

        public GhostButton()
        {
            SetStyle(ControlStyles.OptimizedDoubleBuffer | ControlStyles.AllPaintingInWmPaint |
                     ControlStyles.UserPaint | ControlStyles.ResizeRedraw |
                         ControlStyles.SupportsTransparentBackColor, true);
            Size = new Size(150, 46);
            Cursor = Cursors.Hand;
            MouseEnter += delegate { _hot = true; Invalidate(); };
            MouseLeave += delegate { _hot = false; _down = false; Invalidate(); };
            MouseDown += delegate { _down = true; Invalidate(); };
            MouseUp += delegate { _down = false; Invalidate(); };
        }

        protected override void OnPaint(PaintEventArgs e)
        {
            var g = e.Graphics;
            Theme.HiQuality(g);

            int inset = _down ? 1 : 0;
            var rr = new Rectangle(inset, inset, Width - 1 - inset * 2, Height - 2 - inset * 2);

            using (var p = Theme.RoundRect(rr, Radius))
            using (var b = new SolidBrush(_hot ? Color.FromArgb(0xF2, 0xF5, 0xFA) : Theme.Card))
                g.FillPath(b, p);
            using (var p = Theme.RoundRect(rr, Radius))
            using (var pen = new Pen(_hot ? Color.FromArgb(0xC2, 0xCE, 0xE2) : Theme.Line, 1f))
                g.DrawPath(pen, p);

            string t = Text ?? "";
            var sz = g.MeasureString(t, Theme.F10);
            using (var b = new SolidBrush(Theme.TextSub))
                g.DrawString(t, Theme.F10, b, (Width - sz.Width) / 2f, (Height - sz.Height) / 2f);
        }
    }

    public class SelectCard : Control
    {
        private bool _hot;
        public bool Selected;
        public string SubText = "";

        public SelectCard()
        {
            SetStyle(ControlStyles.OptimizedDoubleBuffer | ControlStyles.AllPaintingInWmPaint |
                     ControlStyles.UserPaint | ControlStyles.ResizeRedraw |
                         ControlStyles.SupportsTransparentBackColor, true);
            Cursor = Cursors.Hand;
            MouseEnter += delegate { _hot = true; Invalidate(); };
            MouseLeave += delegate { _hot = false; Invalidate(); };
        }

        protected override void OnPaint(PaintEventArgs e)
        {
            var g = e.Graphics;
            Theme.HiQuality(g);

            int lift = (_hot && !Selected) ? 1 : 0;
            var rr = new Rectangle(0, 1 - lift, Width - 1, Height - 3);

            if (Selected)
            {
                using (var p = Theme.RoundRect(new Rectangle(0, 3, Width - 1, Height - 3), 12))
                using (var b = new SolidBrush(Color.FromArgb(10, 0x2F, 0x6B, 0xF0)))
                    g.FillPath(b, p);
            }
            else if (_hot)
            {
                using (var p = Theme.RoundRect(new Rectangle(0, 3, Width - 1, Height - 3), 12))
                using (var b = new SolidBrush(Color.FromArgb(8, 0x00, 0x00, 0x00)))
                    g.FillPath(b, p);
            }

            using (var p = Theme.RoundRect(rr, 12))
            using (var b = new SolidBrush(Selected ? Theme.AccentSoft : (_hot ? Theme.CardAlt : Theme.Card)))
                g.FillPath(b, p);

            using (var p = Theme.RoundRect(rr, 12))
            using (var pen = new Pen(Selected ? Theme.Accent : (_hot ? Color.FromArgb(0xC2, 0xCE, 0xE2) : Theme.Line),
                                     Selected ? 1.6f : 1f))
                g.DrawPath(pen, p);

            var dot = new Rectangle(Width - 30, Height / 2 - 16, 18, 18);
            if (Selected)
            {
                using (var b = new SolidBrush(Theme.Accent))
                    g.FillEllipse(b, dot);
                Icons.Check(g, new Rectangle(dot.X - 1, dot.Y - 1, dot.Width + 2, dot.Height + 2),
                            Color.White, 1.8f);
            }
            else
            {
                using (var pen = new Pen(Color.FromArgb(0xCF, 0xD6, 0xE2), 1.4f))
                    g.DrawEllipse(pen, dot);
            }

            int textLeft = 16;
            int textWidth = Width - textLeft - 40;
            using (var b = new SolidBrush(Selected ? Theme.Accent : Theme.Text))
                g.DrawString(Text ?? "", Theme.FB9, b,
                    new RectangleF(textLeft, 15, textWidth, 20));
            if (SubText.Length > 0)
            {
                using (var b = new SolidBrush(Selected ? Color.FromArgb(0x4A, 0x72, 0xC8) : Theme.TextHint))
                    g.DrawString(SubText, Theme.F8, b,
                        new RectangleF(textLeft, 37, textWidth, 34));
            }
        }
    }

    public class NavItem : Control
    {
        private bool _hot;
        public bool Active;
        public int IconKind;  

        public NavItem()
        {
            SetStyle(ControlStyles.OptimizedDoubleBuffer | ControlStyles.AllPaintingInWmPaint |
                     ControlStyles.UserPaint | ControlStyles.ResizeRedraw |
                         ControlStyles.SupportsTransparentBackColor, true);
            Height = 44;
            Cursor = Cursors.Hand;
            MouseEnter += delegate { _hot = true; Invalidate(); };
            MouseLeave += delegate { _hot = false; Invalidate(); };
        }

        protected override void OnPaint(PaintEventArgs e)
        {
            var g = e.Graphics;
            Theme.HiQuality(g);

            var rr = new Rectangle(10, 2, Width - 20, Height - 5);
            if (Active)
            {
                using (var p = Theme.RoundRect(rr, 10))
                using (var b = new SolidBrush(Theme.AccentSoft))
                    g.FillPath(b, p);
                using (var b = new SolidBrush(Theme.Accent))
                    g.FillRectangle(b, 6, Height / 2 - 8, 3, 16);
            }
            else if (_hot)
            {
                using (var p = Theme.RoundRect(rr, 10))
                using (var b = new SolidBrush(Color.FromArgb(0xF0, 0xF3, 0xF9)))
                    g.FillPath(b, p);
            }

            var ir = new Rectangle(24, Height / 2 - 9, 18, 18);
            Color ic = Active ? Theme.Accent : Theme.TextSub;
            switch (IconKind)
            {
                case 0: Icons.Home(g, ir, ic); break;
                case 1: Icons.Gear(g, ir, ic); break;
                case 2: Icons.Book(g, ir, ic); break;
                default: Icons.Info(g, ir, ic); break;
            }

            using (var b = new SolidBrush(Active ? Theme.Accent : Theme.TextSub))
                g.DrawString(Text ?? "", Active ? Theme.FB9 : Theme.F9, b, 52, Height / 2 - 10);
        }
    }

    public class TitleBar : Control
    {
        private Form _form;
        private Point _drag;
        private bool _dragging;
        private int _hotBtn = -1;

        public TitleBar(Form form)
        {
            SetStyle(ControlStyles.OptimizedDoubleBuffer | ControlStyles.AllPaintingInWmPaint |
                     ControlStyles.UserPaint | ControlStyles.ResizeRedraw |
                         ControlStyles.SupportsTransparentBackColor, true);
            _form = form;
            Height = 46;
            Dock = DockStyle.None;
        }

        private Rectangle BtnRect(int i)
        {
            int w = 46;
            return new Rectangle(Width - w * (3 - i), 0, w, Height);
        }

        protected override void OnMouseMove(MouseEventArgs e)
        {
            base.OnMouseMove(e);
            int old = _hotBtn;
            _hotBtn = -1;
            for (int i = 0; i < 3; i++)
                if (BtnRect(i).Contains(e.Location)) _hotBtn = i;

            if (_dragging)
            {
                var p = _form.PointToScreen(e.Location);
                _form.Location = new Point(p.X - _drag.X, p.Y - _drag.Y);
            }
            else if (old != _hotBtn) Invalidate();
        }

        protected override void OnMouseDown(MouseEventArgs e)
        {
            base.OnMouseDown(e);
            if (_hotBtn >= 0) return;
            _dragging = true;
            _drag = e.Location;
        }

        protected override void OnMouseUp(MouseEventArgs e)
        {
            base.OnMouseUp(e);
            _dragging = false;
            if (_hotBtn == 0) _form.WindowState = FormWindowState.Minimized;
            else if (_hotBtn == 1)
                _form.WindowState = _form.WindowState == FormWindowState.Maximized
                    ? FormWindowState.Normal : FormWindowState.Maximized;
            else if (_hotBtn == 2) _form.Close();
        }

        protected override void OnMouseLeave(EventArgs e)
        {
            base.OnMouseLeave(e);
            _hotBtn = -1;
            _dragging = false;
            Invalidate();
        }

        protected override void OnPaint(PaintEventArgs e)
        {
            var g = e.Graphics;
            Theme.HiQuality(g);

            using (var b = new SolidBrush(Color.White))
                g.FillRectangle(b, ClientRectangle);
            using (var pen = new Pen(Theme.LineSoft, 1f))
                g.DrawLine(pen, 0, Height - 1, Width, Height - 1);

            var ic = new Rectangle(20, Height / 2 - 11, 22, 22);
            using (var p = Theme.RoundRect(ic, 7))
            using (var b = Theme.Gradient(ic))
                g.FillPath(b, p);
            using (var pen = new Pen(Color.White, 1.8f))
            {
                pen.StartCap = LineCap.Round; pen.EndCap = LineCap.Round;
                g.DrawLine(pen, ic.X + 7, ic.Y + 11, ic.Right - 6, ic.Y + 11);
                g.DrawLine(pen, ic.Right - 10, ic.Y + 7, ic.Right - 6, ic.Y + 11);
                g.DrawLine(pen, ic.Right - 10, ic.Y + 15, ic.Right - 6, ic.Y + 11);
            }

            using (var b = new SolidBrush(Theme.Text))
                g.DrawString("信息传输助手", Theme.FB11, b, 52, Height / 2 - 11);
            using (var b = new SolidBrush(Theme.TextHint))
                g.DrawString("配置生成器", Theme.F9, b, 150, Height / 2 - 9);

            using (var b = new SolidBrush(Theme.TextHint))
            {
                var sz = g.MeasureString(Theme.Engineer, Theme.F8);
                float right = Width - 46 * 3 - 12;
                float tx = right - sz.Width;
                if (tx > 220)
                {
                    var rect = new RectangleF(tx, Height / 2 - 9, sz.Width + 2, 20);
                    using (var sf = new StringFormat())
                    {
                        sf.Alignment = StringAlignment.Far;
                        sf.LineAlignment = StringAlignment.Center;
                        g.DrawString(Theme.Engineer, Theme.F8, b, rect, sf);
                    }
                }
            }

            for (int i = 0; i < 3; i++)
            {
                var r = BtnRect(i);
                if (_hotBtn == i)
                {
                    Color bg = (i == 2) ? Color.FromArgb(0xE8, 0x11, 0x23) : Color.FromArgb(0xF1, 0xF4, 0xF9);
                    using (var b = new SolidBrush(bg)) g.FillRectangle(b, r);
                }
                Color fg = (_hotBtn == i && i == 2) ? Color.White :
                           (_hotBtn == i ? Theme.Text : Theme.TextSub);
                int cx = r.X + r.Width / 2, cy = r.Y + r.Height / 2;
                using (var pen = new Pen(fg, 1.4f))
                {
                    if (i == 0) g.DrawLine(pen, cx - 6, cy, cx + 6, cy);
                    else if (i == 1) g.DrawRectangle(pen, cx - 5, cy - 5, 10, 10);
                    else
                    {
                        g.DrawLine(pen, cx - 5, cy - 5, cx + 5, cy + 5);
                        g.DrawLine(pen, cx + 5, cy - 5, cx - 5, cy + 5);
                    }
                }
            }
        }
    }

    public class LogBox : Panel
    {
        public readonly RichTextBox Inner;

        public LogBox()
        {
            SetStyle(ControlStyles.OptimizedDoubleBuffer | ControlStyles.AllPaintingInWmPaint |
                     ControlStyles.UserPaint | ControlStyles.ResizeRedraw |
                     ControlStyles.SupportsTransparentBackColor, true);
            BackColor = Color.Transparent;

            Inner = new RichTextBox();
            Inner.ReadOnly = true;
            Inner.BorderStyle = BorderStyle.None;
            Inner.BackColor = Theme.LogBg;
            Inner.ForeColor = Color.FromArgb(0xB8, 0xD8, 0xC0);
            Inner.Font = Theme.Mono95;
            Inner.DetectUrls = false;
            Inner.ScrollBars = RichTextBoxScrollBars.Vertical;
            Controls.Add(Inner);
        }

        protected override void OnResize(EventArgs e)
        {
            base.OnResize(e);
            if (Inner == null) return;
            Inner.SetBounds(14, 10, Math.Max(20, Width - 28), Math.Max(20, Height - 20));
        }

        protected override void OnPaint(PaintEventArgs e)
        {
            var g = e.Graphics;
            Theme.HiQuality(g);
            using (var p = Theme.RoundRect(new Rectangle(0, 0, Width - 1, Height - 1), 10))
            using (var b = new SolidBrush(Theme.LogBg))
                g.FillPath(b, p);
        }
    }

    public class HelpBox : Panel
    {
        public readonly RichTextBox Inner;

        public HelpBox()
        {
            SetStyle(ControlStyles.OptimizedDoubleBuffer | ControlStyles.AllPaintingInWmPaint |
                     ControlStyles.UserPaint | ControlStyles.ResizeRedraw |
                     ControlStyles.SupportsTransparentBackColor, true);
            BackColor = Color.Transparent;

            Inner = new RichTextBox();
            Inner.ReadOnly = true;
            Inner.BorderStyle = BorderStyle.None;
            Inner.BackColor = Theme.Card;
            Inner.ForeColor = Theme.Text;
            Inner.Font = Theme.F10;
            Inner.DetectUrls = false;
            Inner.ScrollBars = RichTextBoxScrollBars.Vertical;
            Controls.Add(Inner);
        }

        protected override void OnResize(EventArgs e)
        {
            base.OnResize(e);
            if (Inner == null) return;
            Inner.SetBounds(22, 18, Math.Max(20, Width - 44), Math.Max(20, Height - 36));
        }

        protected override void OnPaint(PaintEventArgs e)
        {
            var g = e.Graphics;
            Theme.HiQuality(g);
            var r = new Rectangle(0, 0, Width - 1, Height - 1);
            using (var p = Theme.RoundRect(r, 14))
            using (var b = new SolidBrush(Theme.Card))
                g.FillPath(b, p);
            using (var p = Theme.RoundRect(r, 14))
            using (var pen = new Pen(Theme.Line, 1f))
                g.DrawPath(pen, p);
        }
    }

    public class MainForm : Form
    {
       
        private InputBox inpHost, inpPort, inpKey;
        private SelectCard cardOss, cardAuto, cardIpv6;
        private ToggleSwitch swAutostart, swOpenFolder;
        private PrimaryButton btnGen;
        private GhostButton btnOutDir, btnPickDir;
        private Label lblStatus;
        private RichTextBox txtLog;

        private InputBox inpOutDir;
        private ToggleSwitch swSettingOpen;
        private Card c2;

        private NavItem navHome, navSet, navHelp, navAbout;
        private Panel pageHost;
        private Panel pageHome, pageSet, pageHelp, pageAbout;
        private Panel overlay;         

        private string _outDir = "";

        public MainForm()
        {
            SetStyle(ControlStyles.OptimizedDoubleBuffer | ControlStyles.AllPaintingInWmPaint, true);
            Text = "信息传输助手 —— 生成器";
            FormBorderStyle = FormBorderStyle.None;
            StartPosition = FormStartPosition.CenterScreen;
            ClientSize = new Size(980, 720);
            DoubleBuffered = true;
            Font = Theme.F9;
            BackColor = Theme.Bg;
            KeyPreview = true;
            MinimumSize = new Size(900, 660);

            _outDir = BatBuilder.DefaultOutDir();

            BuildChrome();
            BuildHomePage();
            BuildSetPage();
            BuildHelpPage();
            BuildAboutPage();
            ShowPage(0);

            KeyDown += delegate(object s, KeyEventArgs e)
            {
                if (e.KeyCode == Keys.Escape) Close();
            };
        }

        protected override void OnPaintBackground(PaintEventArgs e)
        {
            var g = e.Graphics;
            Theme.HiQuality(g);
            using (var b = new SolidBrush(Theme.Bg))
                g.FillRectangle(b, ClientRectangle);

            DrawGlow(g, new Rectangle(-160, -140, 520, 520), 70, Theme.Accent);
            DrawGlow(g, new Rectangle(ClientSize.Width - 380, ClientSize.Height - 300, 520, 520), 52, Theme.Accent2);
        }

        private static void DrawGlow(Graphics g, Rectangle r, int alpha, Color tint)
        {
            using (var p = new GraphicsPath())
            {
                p.AddEllipse(r);
                using (var b = new PathGradientBrush(p))
                {
                    b.CenterColor = Color.FromArgb(alpha, tint);
                    b.SurroundColors = new Color[] { Color.FromArgb(0, tint) };
                    g.FillPath(b, p);
                }
            }
        }

        private void BuildChrome()
        {
            var bar = new TitleBar(this);
            bar.SetBounds(0, 0, ClientSize.Width, 46);
            bar.Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right;
            Controls.Add(bar);

            var nav = new Panel();
            nav.BackColor = Color.Transparent;
            nav.Width = 196;
            nav.Paint += delegate(object s, PaintEventArgs e)
            {
                using (var pen = new Pen(Theme.LineSoft, 1f))
                    e.Graphics.DrawLine(pen, nav.Width - 1, 0, nav.Width - 1, nav.Height);
            };
            nav.SetBounds(0, 46, 196, ClientSize.Height - 46);
            nav.Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Bottom;
            Controls.Add(nav);

            navHome = MakeNav("主页", 0, 24);
            navSet = MakeNav("设置", 1, 24 + 48);
            navHelp = MakeNav("使用说明", 2, 24 + 48 * 2);
            navAbout = MakeNav("关于", 3, 24 + 48 * 3);
            nav.Controls.Add(navHome);
            nav.Controls.Add(navSet);
            nav.Controls.Add(navHelp);
            nav.Controls.Add(navAbout);

            navHome.Click += delegate { ShowPage(0); };
            navSet.Click += delegate { ShowPage(1); };
            navHelp.Click += delegate { ShowPage(2); };
            navAbout.Click += delegate { ShowPage(3); };

            var ver = new Label();
            ver.Text = "v1.0  ·  单文件免安装";
            ver.Font = Theme.F8;
            ver.ForeColor = Theme.TextHint;
            ver.AutoSize = false;
            ver.Height = 20;
            ver.Dock = DockStyle.Bottom;
            ver.TextAlign = ContentAlignment.MiddleCenter;
            ver.Margin = new Padding(0, 0, 0, 14);
            nav.Controls.Add(ver);

            pageHost = new Panel();
            pageHost.BackColor = Color.Transparent;
            pageHost.SetBounds(196, 46, ClientSize.Width - 196, ClientSize.Height - 46);
            pageHost.Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right | AnchorStyles.Bottom;
            Controls.Add(pageHost);
            pageHost.BringToFront();

            overlay = new Panel();
            overlay.Dock = DockStyle.Fill;
            overlay.BackColor = Theme.Bg;
            overlay.Visible = false;
            pageHost.Controls.Add(overlay);

            Resize += delegate { ApplyRoundRegion(); };
            ApplyRoundRegion();
        }

        private void ApplyRoundRegion()
        {
            if (WindowState == FormWindowState.Maximized)
            {
                Region = null;
                return;
            }
            using (var p = Theme.RoundRect(new Rectangle(0, 0, Width, Height), 16))
                Region = new Region(p);
        }

        private NavItem MakeNav(string text, int kind, int top)
        {
            var n = new NavItem();
            n.Text = text;
            n.IconKind = kind;
            n.Left = 0;
            n.Top = 0;
            n.Width = 196;
            n.Location = new Point(0, top);
            return n;
        }

        private void ShowPage(int idx)
        {
            if (pageHome == null) return;

            Panel target = idx == 0 ? pageHome : idx == 1 ? pageSet : idx == 2 ? pageHelp : pageAbout;
            if (target == null) return;

            pageHome.Visible = idx == 0;
            pageSet.Visible = idx == 1;
            pageHelp.Visible = idx == 2;
            pageAbout.Visible = idx == 3;

            navHome.Active = idx == 0;
            navSet.Active = idx == 1;
            navHelp.Active = idx == 2;
            navAbout.Active = idx == 3;
            navHome.Invalidate(); navSet.Invalidate();
            navHelp.Invalidate(); navAbout.Invalidate();

            FadeIn(target);
        }

        private void FadeIn(Panel target)
        {
            overlay.Bounds = target.Bounds;
            overlay.Parent = target.Parent;
            overlay.Visible = true;
            overlay.BringToFront();

            int step = 0;
            var t = new Timer();
            t.Interval = 16;
            t.Tick += delegate
            {
                step++;
                int a = 255 - step * 52;
                if (a <= 0)
                {
                    t.Stop();
                    t.Dispose();
                    overlay.Visible = false;
                    return;
                }
                overlay.BackColor = Color.FromArgb(a, Theme.Bg);
            };
            t.Start();
        }

        private Label lbPortCap, lbKeyCap, lbHint1, lbHint2, lbSw1Cap, lbSw1Hint, lbSw2Cap;
        private Card cHost, cKey, cTransport, cOptions;

        private void BuildHomePage()
        {
            pageHome = NewPagePanel();
            pageHome.Resize += delegate { LayoutHome(); };

            cHost = new Card();
            cHost.Title = "服务器地址";
            pageHome.Controls.Add(cHost);

            lbHint1 = new Label();
            lbHint1.Text = "只填 IP 或域名，不要带 http:// 和路径";
            lbHint1.Font = Theme.F8;
            lbHint1.ForeColor = Theme.TextHint;
            lbHint1.AutoSize = true;
            cHost.Controls.Add(lbHint1);

            inpHost = new InputBox();
            inpHost.UseIcon = true;
            inpHost.Placeholder = "203.0.113.10  或  relay.example.com";
            cHost.Controls.Add(inpHost);
            inpHost.Submit += delegate { inpPort.Inner.Focus(); };

            cKey = new Card();
            pageHome.Controls.Add(cKey);

            lbPortCap = new Label();
            lbPortCap.Text = "端口";
            lbPortCap.Font = Theme.FB9;
            lbPortCap.ForeColor = Theme.TextSub;
            lbPortCap.AutoSize = true;
            cKey.Controls.Add(lbPortCap);

            inpPort = new InputBox();
            inpPort.Placeholder = "8080";
            inpPort.ValueText = "8080";
            cKey.Controls.Add(inpPort);

            lbKeyCap = new Label();
            lbKeyCap.Text = "设备密钥";
            lbKeyCap.Font = Theme.FB9;
            lbKeyCap.ForeColor = Theme.TextSub;
            lbKeyCap.AutoSize = true;
            cKey.Controls.Add(lbKeyCap);

            inpKey = new InputBox();
            inpKey.IsPassword = true;
            inpKey.Placeholder = "32 位随机串";
            cKey.Controls.Add(inpKey);

            lbHint2 = new Label();
            lbHint2.Text = "安装完会打印在屏幕上；忘了可在控制台「账号」页复制";
            lbHint2.Font = Theme.F8;
            lbHint2.ForeColor = Theme.TextHint;
            lbHint2.AutoSize = true;
            cKey.Controls.Add(lbHint2);

            inpPort.Inner.KeyPress += delegate(object s2, KeyPressEventArgs e)
            {
                if (!char.IsDigit(e.KeyChar) && e.KeyChar != (char)8) e.Handled = true;
            };

            cTransport = new Card();
            cTransport.Title = "传输方式";
            pageHome.Controls.Add(cTransport);

            cardOss = NewSelectCard("仅 OSS", "服务器中转，兼容性最好\n任何网络都能用");
            cardAuto = NewSelectCard("IPv6 优先", "直连最快，免 OSS 流量\n不通时自动回落 OSS");
            cardIpv6 = NewSelectCard("仅 IPv6", "只用直连\n不通就报错，不产生 OSS 费用");
            cTransport.Controls.Add(cardOss);
            cTransport.Controls.Add(cardAuto);
            cTransport.Controls.Add(cardIpv6);
            cardOss.Selected = true;

            cOptions = new Card();
            pageHome.Controls.Add(cOptions);

            swAutostart = new ToggleSwitch();
            swAutostart.Checked = true;
            cOptions.Controls.Add(swAutostart);

            lbSw1Cap = new Label();
            lbSw1Cap.Text = "开机自动启动";
            lbSw1Cap.Font = Theme.F9;
            lbSw1Cap.ForeColor = Theme.Text;
            lbSw1Cap.AutoSize = true;
            cOptions.Controls.Add(lbSw1Cap);

            lbSw1Hint = new Label();
            lbSw1Hint.Text = "下次登录自动连回来";
            lbSw1Hint.Font = Theme.F8;
            lbSw1Hint.ForeColor = Theme.TextHint;
            lbSw1Hint.AutoSize = true;
            cOptions.Controls.Add(lbSw1Hint);

            swOpenFolder = new ToggleSwitch();
            swOpenFolder.Checked = true;
            cOptions.Controls.Add(swOpenFolder);

            lbSw2Cap = new Label();
            lbSw2Cap.Text = "生成后打开所在文件夹";
            lbSw2Cap.Font = Theme.F9;
            lbSw2Cap.ForeColor = Theme.Text;
            lbSw2Cap.AutoSize = true;
            cOptions.Controls.Add(lbSw2Cap);

            btnGen = new PrimaryButton();
            btnGen.Text = "生成传输助手";
            btnGen.Click += delegate { OnGenerate(); };
            pageHome.Controls.Add(btnGen);

            btnOutDir = new GhostButton();
            btnOutDir.Text = "打开输出目录";
            btnOutDir.Click += delegate { OpenDir(_outDir); };
            pageHome.Controls.Add(btnOutDir);

            lblStatus = new Label();
            lblStatus.Text = "● 等待填写";
            lblStatus.Font = Theme.F9;
            lblStatus.ForeColor = Theme.TextHint;
            lblStatus.AutoSize = true;
            pageHome.Controls.Add(lblStatus);

            var logBox = new LogBox();
            pageHome.Controls.Add(logBox);
            txtLog = logBox.Inner;

            Log("准备就绪。");
            Log("服务器地址只填 IP 或域名，端口单独填。");
        }

        private void LayoutHome()
        {
            if (pageHome == null || cHost == null) return;
            int pad = 26;
            int w = pageHome.Width - pad * 2;
            if (w < 300) return;             

            int y = 22;

            cHost.SetBounds(pad, y, w, 96);
            inpHost.SetBounds(18, 32, w - 36, 44);
            lbHint1.Location = new Point(18, 78);
            y += 96 + 12;

            cKey.SetBounds(pad, y, w, 96);
            lbPortCap.Location = new Point(18, 12);
            inpPort.SetBounds(18, 34, 132, 44);
            lbKeyCap.Location = new Point(168, 12);
            int keyW = w - 168 - 18;
            if (keyW < 120) keyW = 120;
            inpKey.SetBounds(168, 34, keyW, 44);
            lbHint2.Location = new Point(168, 80);
            y += 96 + 12;

            cTransport.SetBounds(pad, y, w, 126);
            int cw = (w - 36 - 24) / 3;
            if (cw < 80) cw = 80;
            cardOss.SetBounds(18, 44, cw, 74);
            cardAuto.SetBounds(18 + cw + 12, 44, cw, 74);
            cardIpv6.SetBounds(18 + (cw + 12) * 2, 44, cw, 74);
            y += 126 + 12;

            cOptions.SetBounds(pad, y, w, 62);
            swAutostart.Location = new Point(18, 18);
            lbSw1Cap.Location = new Point(74, 22);
            lbSw1Hint.Location = new Point(178, 24);
            swOpenFolder.Location = new Point(364, 18);
            lbSw2Cap.Location = new Point(420, 22);
            y += 62 + 14;

            btnGen.SetBounds(pad, y, 240, 46);
            btnOutDir.SetBounds(pad + 252, y, 160, 46);
            y += 46 + 14;

            lblStatus.Location = new Point(pad + 2, y);
            y += 24;

            int logH = pageHome.Height - y - 18;
            if (logH < 90) logH = 90;
            foreach (Control c in pageHome.Controls)
            {
                if (c is LogBox) c.SetBounds(pad, y, w, logH);
            }
        }

        private SelectCard NewSelectCard(string title, string sub)
        {
            var c = new SelectCard();
            c.Text = title;
            c.SubText = sub;
            c.Click += delegate { PickTransport(c); };
            return c;
        }

        private void PickTransport(SelectCard c)
        {
            cardOss.Selected = (c == cardOss);
            cardAuto.Selected = (c == cardAuto);
            cardIpv6.Selected = (c == cardIpv6);
            cardOss.Invalidate(); cardAuto.Invalidate(); cardIpv6.Invalidate();
        }

        private string SelectedTransport()
        {
            if (cardAuto != null && cardAuto.Selected) return BatBuilder.TransportAuto;
            if (cardIpv6 != null && cardIpv6.Selected) return BatBuilder.TransportIpv6;
            return BatBuilder.TransportOss;
        }

        private Panel NewPagePanel()
        {
            var p = new Panel();
            p.Dock = DockStyle.Fill;
            p.BackColor = Color.Transparent;
            p.Visible = false;
            p.Resize += delegate
            {
                foreach (Control c in p.Controls)
                {
                    if (c is Card) c.Width = p.Width - 52;
                }
            };
            pageHost.Controls.Add(p);
            return p;
        }

        private void BuildSetPage()
        {
            pageSet = NewPagePanel();
            int pad = 26;
            int y = pad;

            var c = new Card();
            c.Title = "输出位置";
            c.Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right;
            pageSet.Controls.Add(c);
            pageSet.Resize += delegate
            {
                int w = pageSet.Width - 52;
                if (w < 300) return;
                c.SetBounds(26, 22, w, 150);
                inpOutDir.SetBounds(18, 68, w - 140, 44);
                btnPickDir.SetBounds(w - 112, 68, 94, 44);
                c2.SetBounds(26, 22 + 150 + 14, w, 66);
            };

            var lb = new Label();
            lb.Text = "生成的 传输助手.bat 保存到";
            lb.Font = Theme.F9;
            lb.ForeColor = Theme.TextSub;
            lb.AutoSize = true;
            lb.Location = new Point(18, 44);
            c.Controls.Add(lb);

            inpOutDir = new InputBox();
            inpOutDir.ValueText = _outDir;
            c.Controls.Add(inpOutDir);

            btnPickDir = new GhostButton();
            btnPickDir.Text = "浏览…";
            btnPickDir.Click += delegate { PickOutDir(); };
            c.Controls.Add(btnPickDir);

            y += 150 + 14;

            c2 = new Card();
            c2.Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right;
            pageSet.Controls.Add(c2);

            swSettingOpen = new ToggleSwitch();
            swSettingOpen.Left = 18;
            swSettingOpen.Top = 20;
            swSettingOpen.Checked = true;
            c2.Controls.Add(swSettingOpen);

            var lb2 = new Label();
            lb2.Text = "生成成功后自动打开所在文件夹";
            lb2.Font = Theme.F9;
            lb2.ForeColor = Theme.Text;
            lb2.AutoSize = true;
            lb2.Location = new Point(74, 24);
            c2.Controls.Add(lb2);

            swSettingOpen.Changed += delegate { if (swOpenFolder != null) swOpenFolder.Checked = swSettingOpen.Checked; };
            if (swOpenFolder != null) swOpenFolder.Changed += delegate { swSettingOpen.Checked = swOpenFolder.Checked; };
        }

        private void PickOutDir()
        {
            using (var d = new FolderBrowserDialog())
            {
                d.Description = "选择 传输助手.bat 的保存位置";
                if (Directory.Exists(inpOutDir.ValueText)) d.SelectedPath = inpOutDir.ValueText;
                if (d.ShowDialog(this) == DialogResult.OK)
                {
                    inpOutDir.ValueText = d.SelectedPath;
                    _outDir = d.SelectedPath;
                }
            }
        }

        private void BuildHelpPage()
        {
            pageHelp = NewPagePanel();

            HelpBox box = new HelpBox();
            pageHelp.Controls.Add(box);
            pageHelp.Resize += delegate
            {
                int pad = 26;
                box.SetBounds(pad, pad, pageHelp.Width - pad * 2, pageHelp.Height - pad * 2);
            };

            var sb = new StringBuilder();
            sb.AppendLine("使用说明");
            sb.AppendLine();
            sb.AppendLine("【三步上手】");
            sb.AppendLine();
            sb.AppendLine("1. 先去服务器上把中继装好（见《快速开始》），装完屏幕上会打印");
            sb.AppendLine("   控制台地址、登录密码、设备密钥。");
            sb.AppendLine();
            sb.AppendLine("2. 回到这个窗口，填三项：");
            sb.AppendLine("   · 服务器地址：只填 IP 或域名，不要带 http://");
            sb.AppendLine("   · 端口：默认 8080，没改过就不用动");
            sb.AppendLine("   · 设备密钥：服务器打印的那串 32 位字符");
            sb.AppendLine();
            sb.AppendLine("3. 点「生成传输助手」，得到一个 传输助手.bat。");
            sb.AppendLine("   把这个 bat 发到目标电脑，双击运行即可。");
            sb.AppendLine();
            sb.AppendLine("【传输方式怎么选】");
            sb.AppendLine();
            sb.AppendLine("· 仅 OSS —— 推荐先用这个。文件走 OSS 中转，任何网络环境都能用。");
            sb.AppendLine("· IPv6 优先 —— 双方都有公网 IPv6 时直连最快，还不花 OSS 流量。");
            sb.AppendLine("· 仅 IPv6 —— 只在确定有 IPv6 时用，不通就直接报错，不回落。");
            sb.AppendLine();
            sb.AppendLine("【开机自启】");
            sb.AppendLine();
            sb.AppendLine("勾上之后，目标电脑下次登录会自动连回来，不用再双击 bat。");
            sb.AppendLine("想彻底停止：双击压缩包里的「停止服务.bat」，它会结束后台进程");
            sb.AppendLine("并删掉开机自启项。只关黑窗口是不够的。");
            sb.AppendLine();
            sb.AppendLine("【命令行用法】");
            sb.AppendLine();
            sb.AppendLine("本程序也支持命令行，适合批量生成：");
            sb.AppendLine("  信息传输助手生成器.exe --host 1.2.3.4 --key 密钥 [--port 8080]");
            sb.AppendLine("       [--transport oss|auto|ipv6] [--out 路径] [--no-autostart]");
            sb.AppendLine("不带任何参数则打开图形界面。");
            sb.AppendLine();
            sb.AppendLine("【常见问题】");
            sb.AppendLine();
            sb.AppendLine("Q: 提示「服务器地址只能包含字母、数字、点、短横线」？");
            sb.AppendLine("A: 你多半把 http:// 或路径也粘进去了。只填 203.0.113.10 这样。");
            sb.AppendLine();
            sb.AppendLine("Q: 双击 bat 一闪就没了？");
            sb.AppendLine("A: 看 %LOCALAPPDATA%\\WinUpdSvc\\agent.log 这个日志。");
            sb.AppendLine();
            sb.AppendLine("Q: EXE 报毒？");
            sb.AppendLine("A: 自己编译的 .NET 程序没有数字签名，属误报。可以改用");
            sb.AppendLine("   压缩包里的「配置.txt + 生成传输助手.bat」，效果一样。");

            box.Inner.Tag = sb.ToString();
            box.VisibleChanged += delegate
            {
                if (box.Visible && box.Inner.Tag != null && box.Inner.TextLength == 0)
                {
                    box.Inner.Text = (string)box.Inner.Tag;
                    box.Inner.Select(0, 0);
                }
            };
        }

        private void BuildAboutPage()
        {
            pageAbout = NewPagePanel();
            int pad = 26;

            var c = new Card();
            c.Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right;
            pageAbout.Controls.Add(c);
            pageAbout.Resize += delegate
            {
                c.SetBounds(pad, pad, pageAbout.Width - pad * 2, 250);
            };

            var logo = new LogoBox();
            logo.SetBounds(20, 24, 56, 56);
            c.Controls.Add(logo);

            var t1 = new Label();
            t1.Text = "信息传输助手";
            t1.Font = Theme.FB16;
            t1.ForeColor = Theme.Text;
            t1.AutoSize = true;
            t1.Location = new Point(92, 26);
            c.Controls.Add(t1);

            var t2 = new Label();
            t2.Text = "跨公网大文件传输  ·  配置生成器  v1.0";
            t2.Font = Theme.F9;
            t2.ForeColor = Theme.TextSub;
            t2.AutoSize = true;
            t2.Location = new Point(94, 58);
            c.Controls.Add(t2);

            var t3 = new Label();
            t3.Text = "文件字节不经过你自己的服务器：\r\n" +
                      "数据直传阿里云 OSS，服务器只转发几十字节的控制指令。\r\n" +
                      "所以 1 核 1G 的低配机器也够用，不需要加带宽。";
            t3.Font = Theme.F9;
            t3.ForeColor = Theme.TextSub;
            t3.AutoSize = true;
            t3.Location = new Point(20, 100);
            c.Controls.Add(t3);

            var t4 = new Label();
            t4.Text = "本程序为免安装单文件，不写注册表、不驻留后台。\r\n" +
                      "生成物是纯文本 bat，可自行审计内容。";
            t4.Font = Theme.F8;
            t4.ForeColor = Theme.TextHint;
            t4.AutoSize = true;
            t4.Location = new Point(20, 166);
            c.Controls.Add(t4);

            var t5 = new Label();
            t5.Text = Theme.Engineer;
            t5.Font = Theme.FB9;
            t5.ForeColor = Theme.Accent;
            t5.AutoSize = true;
            t5.Location = new Point(20, 212);
            c.Controls.Add(t5);
        }

        private class LogoBox : Control
        {
            public LogoBox()
            {
                SetStyle(ControlStyles.OptimizedDoubleBuffer | ControlStyles.AllPaintingInWmPaint |
                         ControlStyles.UserPaint | ControlStyles.ResizeRedraw |
                         ControlStyles.SupportsTransparentBackColor, true);
                BackColor = Color.Transparent;
            }

            protected override void OnPaint(PaintEventArgs e)
            {
                var g = e.Graphics;
                Theme.HiQuality(g);
                var r = new Rectangle(0, 0, Width - 1, Height - 1);
                using (var p = Theme.RoundRect(r, 16))
                using (var b = Theme.Gradient(r))
                    g.FillPath(b, p);
                using (var pen = new Pen(Color.White, 2.6f))
                {
                    pen.StartCap = LineCap.Round; pen.EndCap = LineCap.Round;
                    int cy = Height / 2;
                    g.DrawLine(pen, 16, cy, Width - 16, cy);
                    g.DrawLine(pen, Width - 24, cy - 8, Width - 16, cy);
                    g.DrawLine(pen, Width - 24, cy + 8, Width - 16, cy);
                }
            }
        }

        private void Log(string msg)
        {
            if (txtLog == null) return;
            txtLog.AppendText(msg + "\r\n");
        }

        private void SetStatus(string msg, Color c)
        {
            if (lblStatus == null) return;
            lblStatus.Text = "● " + msg;
            lblStatus.ForeColor = c;
        }

        private void OpenDir(string dir)
        {
            try
            {
                if (Directory.Exists(dir))
                    System.Diagnostics.Process.Start("explorer.exe", "\"" + dir + "\"");
                else
                    System.Diagnostics.Process.Start("explorer.exe", "\"" + Path.GetDirectoryName(dir) + "\"");
            }
            catch (Exception ex) { Log("打开目录失败: " + ex.Message); }
        }

        private void OnGenerate()
        {
            btnGen.Enabled2 = false;
            btnGen.Invalidate();
            txtLog.Clear();
            SetStatus("正在生成…", Theme.TextHint);

            try
            {
                string dir = inpOutDir != null ? inpOutDir.ValueText.Trim() : _outDir;
                if (dir.Length == 0) dir = _outDir;
                _outDir = dir;
                string outPath = Path.Combine(dir, "传输助手.bat");

                var r = BatBuilder.Build(inpHost.ValueText, inpPort.ValueText, inpKey.ValueText,
                                         swAutostart.Checked, SelectedTransport(), outPath);
                if (!r.Ok)
                {
                    SetStatus(r.Error, Theme.Err);
                    Log("✗ " + r.Error);
                    return;
                }

                SetStatus("生成成功", Theme.Ok);
                Log("✓ 已生成: " + r.OutPath);
                Log("  大小    : " + (r.Size / 1024) + " KB");
                Log("  控制台  : " + r.RelayUrl);
                Log("  传输方式: " + BatBuilder.TransportLabel(r.Transport));
                Log("  开机自启: " + (swAutostart.Checked ? "是" : "否"));
                Log("");
                Log("下一步：把 bat 发到目标电脑双击运行，");
                Log("        然后用浏览器打开 " + r.RelayUrl);

                bool open = swOpenFolder.Checked;
                ShowSuccess(r);
                if (open)
                {
                    try { System.Diagnostics.Process.Start("explorer.exe", "/select,\"" + r.OutPath + "\""); }
                    catch { }
                }
            }
            catch (Exception ex)
            {
                SetStatus("生成失败", Theme.Err);
                Log("✗ 异常: " + ex.Message);
            }
            finally
            {
                btnGen.Enabled2 = true;
                btnGen.Invalidate();
            }
        }

        private void ShowSuccess(BatBuilder.Result r)
        {
            using (var dlg = new SuccessDialog(r.RelayUrl, Path.GetFileName(r.OutPath),
                                               BatBuilder.TransportLabel(r.Transport), r.OutPath))
            {
                dlg.ShowDialog(this);
            }
        }
    }

    public class SuccessDialog : Form
    {
        private readonly string _url;
        private readonly string _fileName;
        private readonly string _transport;
        private readonly string _fullPath;

        private Label _lblCopyState;
        private Timer _fadeTimer;
        private Timer _copyTimer;
        private int _tick;

        public SuccessDialog(string url, string fileName, string transport, string fullPath)
        {
            _url = url;
            _fileName = fileName;
            _transport = transport;
            _fullPath = fullPath;

            FormBorderStyle = FormBorderStyle.None;
            StartPosition = FormStartPosition.CenterParent;
            ClientSize = new Size(520, 470);
            BackColor = Theme.Card;
            DoubleBuffered = true;
            ShowInTaskbar = false;
            KeyPreview = true;

            BuildUi();

            Opacity = 0;
            _fadeTimer = new Timer();
            _fadeTimer.Interval = 16;
            _fadeTimer.Tick += delegate
            {
                _tick++;
                Opacity = Math.Min(1.0, 0.12 * _tick);
                if (Opacity >= 1.0) { _fadeTimer.Stop(); _fadeTimer.Dispose(); }
            };
            Load += delegate { _fadeTimer.Start(); };
            KeyDown += delegate(object s, KeyEventArgs e)
            {
                if (e.KeyCode == Keys.Escape) Close();
            };
        }

        protected override void OnPaintBackground(PaintEventArgs e)
        {
            var g = e.Graphics;
            Theme.HiQuality(g);
            using (var b = new SolidBrush(Theme.Card))
                g.FillRectangle(b, ClientRectangle);
        }

        private void BuildUi()
        {
           
            var lbTitle = new Label();
            lbTitle.Text = "生成完成";
            lbTitle.Font = Theme.FB11;
            lbTitle.ForeColor = Theme.Text;
            lbTitle.AutoSize = true;
            lbTitle.Location = new Point(26, 22);
            Controls.Add(lbTitle);

            var btnX = new CloseButton();
            btnX.SetBounds(ClientSize.Width - 52, 16, 32, 32);
            btnX.Click += delegate { Close(); };
            Controls.Add(btnX);

            var check = new BigCheck();
            check.SetBounds((ClientSize.Width - 76) / 2, 74, 76, 76);
            Controls.Add(check);

            var lbOk = new Label();
            lbOk.Text = "生成成功！";
            lbOk.Font = Theme.FB16;
            lbOk.ForeColor = Theme.Text;
            lbOk.AutoSize = false;
            lbOk.TextAlign = ContentAlignment.MiddleCenter;
            lbOk.SetBounds(20, 164, ClientSize.Width - 40, 34);
            Controls.Add(lbOk);

            var lbSub = new Label();
            lbSub.Text = "你的传输助手已经准备就绪";
            lbSub.Font = Theme.F9;
            lbSub.ForeColor = Theme.TextHint;
            lbSub.AutoSize = false;
            lbSub.TextAlign = ContentAlignment.MiddleCenter;
            lbSub.SetBounds(20, 198, ClientSize.Width - 40, 22);
            Controls.Add(lbSub);

            var card = new Card();
            card.ShowShadow = false;
            card.SetBounds(26, 234, ClientSize.Width - 52, 152);
            Controls.Add(card);

            var lbAddrT = new Label();
            lbAddrT.Text = "控制台地址";
            lbAddrT.Font = Theme.F8;
            lbAddrT.ForeColor = Theme.TextHint;
            lbAddrT.AutoSize = true;
            lbAddrT.Location = new Point(18, 16);
            card.Controls.Add(lbAddrT);

            var lbAddr = new Label();
            lbAddr.Text = _url;
            lbAddr.Font = new Font("Consolas", 10.5F);
            lbAddr.ForeColor = Theme.Accent;
            lbAddr.AutoSize = false;
            lbAddr.SetBounds(18, 36, card.Width - 92, 24);
            lbAddr.Cursor = Cursors.Hand;
            lbAddr.Click += delegate { DoCopy(); };
            card.Controls.Add(lbAddr);

            var btnCopy = new CopyButton();
            btnCopy.SetBounds(card.Width - 62, 30, 44, 34);
            btnCopy.Click += delegate { DoCopy(); };
            card.Controls.Add(btnCopy);

            _lblCopyState = new Label();
            _lblCopyState.Text = "";
            _lblCopyState.Font = Theme.F8;
            _lblCopyState.ForeColor = Theme.Ok;
            _lblCopyState.AutoSize = false;
            _lblCopyState.TextAlign = ContentAlignment.MiddleRight;
            _lblCopyState.SetBounds(card.Width - 130, 36, 62, 24);
            card.Controls.Add(_lblCopyState);

            var sep = new Panel();
            sep.BackColor = Theme.LineSoft;
            sep.SetBounds(18, 72, card.Width - 36, 1);
            card.Controls.Add(sep);

            var lbK1 = new Label();
            lbK1.Text = "传输方式";
            lbK1.Font = Theme.F9;
            lbK1.ForeColor = Theme.TextSub;
            lbK1.AutoSize = true;
            lbK1.Location = new Point(18, 84);
            card.Controls.Add(lbK1);

            var lbV1 = new Label();
            lbV1.Text = _transport;
            lbV1.Font = Theme.F9;
            lbV1.ForeColor = Theme.Text;
            lbV1.AutoSize = false;
            lbV1.TextAlign = ContentAlignment.MiddleRight;
            lbV1.SetBounds(card.Width / 2, 84, card.Width / 2 - 18, 20);
            card.Controls.Add(lbV1);

            var lbK2 = new Label();
            lbK2.Text = "文件名称";
            lbK2.Font = Theme.F9;
            lbK2.ForeColor = Theme.TextSub;
            lbK2.AutoSize = true;
            lbK2.Location = new Point(18, 114);
            card.Controls.Add(lbK2);

            var lbV2 = new Label();
            lbV2.Text = _fileName;
            lbV2.Font = Theme.F9;
            lbV2.ForeColor = Theme.Text;
            lbV2.AutoSize = false;
            lbV2.TextAlign = ContentAlignment.MiddleRight;
            lbV2.SetBounds(card.Width / 2, 114, card.Width / 2 - 18, 20);
            card.Controls.Add(lbV2);

            var lbNext = new Label();
            lbNext.Text = "下一步";
            lbNext.Font = Theme.FB9;
            lbNext.ForeColor = Theme.Text;
            lbNext.AutoSize = true;
            lbNext.Location = new Point(26, 400);
            Controls.Add(lbNext);

            var lbSteps = new Label();
            lbSteps.Text = "1. 把这个 bat 发到目标电脑　　2. 双击运行　　" +
                           "3. 浏览器打开上面的地址";
            lbSteps.Font = Theme.F8;
            lbSteps.ForeColor = Theme.TextHint;
            lbSteps.AutoSize = true;
            lbSteps.Location = new Point(26, 424);
            Controls.Add(lbSteps);

            var btnOpen = new PrimaryButton();
            btnOpen.Text = "打开文件位置";
            btnOpen.SetBounds(26, 452, 200, 44);
            btnOpen.Click += delegate { OpenFileLocation(); };
            Controls.Add(btnOpen);

            var btnDone = new GhostButton();
            btnDone.Text = "完成";
            btnDone.SetBounds(238, 452, 120, 44);
            btnDone.Click += delegate { Close(); };
            Controls.Add(btnDone);

            ClientSize = new Size(520, 512);
            btnOpen.Top = 452; btnDone.Top = 452;
            lbNext.Top = 400; lbSteps.Top = 424;

            using (var p = Theme.RoundRect(new Rectangle(0, 0, ClientSize.Width, ClientSize.Height), 16))
                Region = new Region(p);
        }

        private void DoCopy()
        {
            try
            {
                Clipboard.SetText(_url);
                _lblCopyState.Text = "已复制";
                _lblCopyState.ForeColor = Theme.Ok;
            }
            catch
            {
                _lblCopyState.Text = "复制失败";
                _lblCopyState.ForeColor = Theme.Err;
            }

            if (_copyTimer != null) { _copyTimer.Stop(); _copyTimer.Dispose(); }
            _copyTimer = new Timer();
            _copyTimer.Interval = 1600;
            _copyTimer.Tick += delegate
            {
                _copyTimer.Stop();
                _copyTimer.Dispose();
                _lblCopyState.Text = "";
            };
            _copyTimer.Start();
        }

        private void OpenFileLocation()
        {
            try
            {
                System.Diagnostics.Process.Start("explorer.exe", "/select,\"" + _fullPath + "\"");
            }
            catch
            {
                try { System.Diagnostics.Process.Start("explorer.exe",
                        "\"" + Path.GetDirectoryName(_fullPath) + "\""); }
                catch { }
            }
        }

        private class BigCheck : Control
        {
            public BigCheck()
            {
                SetStyle(ControlStyles.OptimizedDoubleBuffer | ControlStyles.AllPaintingInWmPaint |
                         ControlStyles.UserPaint | ControlStyles.ResizeRedraw |
                         ControlStyles.SupportsTransparentBackColor, true);
                BackColor = Color.Transparent;
            }

            protected override void OnPaint(PaintEventArgs e)
            {
                var g = e.Graphics;
                Theme.HiQuality(g);
                using (var b = new SolidBrush(Theme.OkSoft))
                    g.FillEllipse(b, 0, 0, Width - 1, Height - 1);
                int m = Width / 4;
                using (var b = new SolidBrush(Theme.Ok))
                    g.FillEllipse(b, m / 2 + 2, m / 2 + 2, Width - m - 4, Height - m - 4);
                Icons.Check(g, new Rectangle(Width / 4, Height / 4 - 2, Width / 2, Height / 2),
                            Color.White, 3.2f);
            }
        }

        private class CloseButton : Control
        {
            private bool _hot;

            public CloseButton()
            {
                SetStyle(ControlStyles.OptimizedDoubleBuffer | ControlStyles.AllPaintingInWmPaint |
                         ControlStyles.UserPaint | ControlStyles.ResizeRedraw |
                         ControlStyles.SupportsTransparentBackColor, true);
                BackColor = Color.Transparent;
                Cursor = Cursors.Hand;
                MouseEnter += delegate { _hot = true; Invalidate(); };
                MouseLeave += delegate { _hot = false; Invalidate(); };
            }

            protected override void OnPaint(PaintEventArgs e)
            {
                var g = e.Graphics;
                Theme.HiQuality(g);
                if (_hot)
                    using (var p = Theme.RoundRect(new Rectangle(0, 0, Width - 1, Height - 1), 8))
                    using (var b = new SolidBrush(Color.FromArgb(0xF1, 0xF4, 0xF9)))
                        g.FillPath(b, p);
                Icons.Close(g, new Rectangle(8, 8, Width - 16, Height - 16),
                            _hot ? Theme.Text : Theme.TextHint);
            }
        }

        private class CopyButton : Control
        {
            private bool _hot;

            public CopyButton()
            {
                SetStyle(ControlStyles.OptimizedDoubleBuffer | ControlStyles.AllPaintingInWmPaint |
                         ControlStyles.UserPaint | ControlStyles.ResizeRedraw |
                         ControlStyles.SupportsTransparentBackColor, true);
                BackColor = Color.Transparent;
                Cursor = Cursors.Hand;
                MouseEnter += delegate { _hot = true; Invalidate(); };
                MouseLeave += delegate { _hot = false; Invalidate(); };
            }

            protected override void OnPaint(PaintEventArgs e)
            {
                var g = e.Graphics;
                Theme.HiQuality(g);
                var r = new Rectangle(0, 0, Width - 1, Height - 1);
                using (var p = Theme.RoundRect(r, 8))
                using (var b = new SolidBrush(_hot ? Theme.AccentSoft : Theme.CardAlt))
                    g.FillPath(b, p);
                using (var p = Theme.RoundRect(r, 8))
                using (var pen = new Pen(_hot ? Theme.Accent : Theme.Line, 1f))
                    g.DrawPath(pen, p);
                Icons.Copy(g, new Rectangle(Width / 2 - 8, Height / 2 - 8, 16, 16),
                           _hot ? Theme.Accent : Theme.TextSub);
            }
        }
    }

    public static class Program
    {
        [STAThread]
        public static void Main(string[] argv)
        {
           
            if (argv != null && argv.Length > 0)
            {
                RunCli(argv);
                return;
            }

            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            Application.Run(new MainForm());
        }

        private static void RunCli(string[] argv)
        {
            string host = null, port = "8080", key = null, outPath = null;
            string transport = BatBuilder.TransportOss;
            bool autostart = true;

            for (int i = 0; i < argv.Length; i++)
            {
                string a = argv[i];
                switch (a)
                {
                    case "--host": host = Next(argv, ref i); break;
                    case "--port": port = Next(argv, ref i); break;
                    case "--key": key = Next(argv, ref i); break;
                    case "--out": outPath = Next(argv, ref i); break;
                    case "--transport": transport = Next(argv, ref i); break;
                    case "--no-autostart": autostart = false; break;
                    case "-h":
                    case "--help":
                    default:
                        Console.WriteLine("用法:");
                        Console.WriteLine("  信息传输助手生成器.exe");
                        Console.WriteLine("      打开图形界面");
                        Console.WriteLine("");
                        Console.WriteLine("  信息传输助手生成器.exe --host <地址> --key <密钥> [--port 8080]");
                        Console.WriteLine("                          [--transport oss|auto|ipv6]");
                        Console.WriteLine("                          [--out 输出路径] [--no-autostart]");
                        Console.WriteLine("      命令行生成，不加 --out 时输出到 EXE 同目录");
                        Console.WriteLine("");
                        Console.WriteLine("  --transport 取值:");
                        Console.WriteLine("      oss   仅 OSS（默认）");
                        Console.WriteLine("      auto  IPv6 优先，失败自动回落 OSS");
                        Console.WriteLine("      ipv6  仅 IPv6");
                        Environment.Exit(2);
                        return;
                }
            }

            if (string.IsNullOrEmpty(host) || string.IsNullOrEmpty(key))
            {
                Console.WriteLine("错误: 命令行模式必须给 --host 和 --key");
                Environment.Exit(2);
                return;
            }

            var r = BatBuilder.Build(host, port, key, autostart, transport, outPath);
            if (!r.Ok)
            {
                Console.WriteLine("错误: " + r.Error);
                Environment.Exit(1);
                return;
            }
            Console.WriteLine("已生成: " + r.OutPath);
            Console.WriteLine("  大小    : " + (r.Size / 1024) + " KB");
            Console.WriteLine("  传输方式: " + BatBuilder.TransportLabel(r.Transport));
            Console.WriteLine();
            Console.WriteLine("控制台地址: " + r.RelayUrl);
            Console.WriteLine("  （用浏览器打开这个地址，登录后即可操作目标电脑）");
            Console.WriteLine("  （把这个 bat 发到目标电脑，双击运行）");
        }

        private static string Next(string[] a, ref int i)
        {
            i++;
            return i < a.Length ? a[i] : "";
        }
    }
}
