#!/usr/bin/env python3
"""
Allseis Seismic Data Downloader v2
===================================
Cross-platform replacement for SeisDataManagement.exe

Downloads raw .BIN seismic trace data from Allseis 1C / 1C-LF rack nodes
over Ethernet via FTP. Works on macOS, Linux, and Windows.
No SuperDog dongle required.

Usage:
    python3 allseis_downloader_v2.py                # Interactive mode
    python3 allseis_downloader_v2.py --scan          # Scan devices
    python3 allseis_downloader_v2.py --download-all  # Download all data

Reverse-engineered from SeisDataManagement v1.2.7-251201
"""

import argparse, csv, ftplib, os, re, sys, time, logging, socket, threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import List, Optional, Tuple, Dict

# ─── Configuration ──────────────────────────────────────────────────────────

CFG = dict(
    ftp_user="pi",
    ftp_pass="raspberry",
    ftp_base="/",           # FTP is chrooted — root IS the data area
    ftp_port=21,
    ftp_timeout=10,
    tcp_port=8810,          # Control channel port (nodes connect TO us)
)

# File classification patterns
PAT_CONFIG  = re.compile(r'^(01_CONFI|CONFIG\d*)\.BIN$', re.I)
PAT_TEST    = re.compile(r'^0[23]_TEST[A]?\.BIN$', re.I)
PAT_LOG     = re.compile(r'^.*\.TXT$', re.I)
PAT_APP     = re.compile(r'^APP-.*\.bin$', re.I)
PAT_DATA    = re.compile(r'^((\d{8})|(\d{2}_\d{4})|(NEW\d{5}))\.BIN$', re.I)
PAT_ANY_BIN = re.compile(r'^.+\.BIN$', re.I)
PAT_EMPTY   = re.compile(r'^NEW\d{5}\.BIN$', re.I)

class C:
    _on = hasattr(sys.stdout, 'isatty') and sys.stdout.isatty()
    RST='\033[0m' if _on else ''; B='\033[1m' if _on else ''; D='\033[2m' if _on else ''
    R='\033[91m' if _on else ''; G='\033[92m' if _on else ''; Y='\033[93m' if _on else ''
    BL='\033[94m' if _on else ''; CY='\033[96m' if _on else ''

# ─── Data Models ────────────────────────────────────────────────────────────

@dataclass
class DevCh:
    rack: int; ch: int; ip: str; sub: int
    @property
    def label(self): return f"R{self.rack}:CH{self.ch}"

@dataclass
class RFile:
    name: str; size: int; ftype: str
    @property
    def size_h(self):
        if self.size >= 1<<30: return f"{self.size/(1<<30):.1f} GB"
        if self.size >= 1<<20: return f"{self.size/(1<<20):.1f} MB"
        if self.size >= 1<<10: return f"{self.size/(1<<10):.1f} KB"
        return f"{self.size} B"

@dataclass
class DevInfo:
    ip: str; channels: List[DevCh] = field(default_factory=list)
    files: List[RFile] = field(default_factory=list)
    online: bool = False; error: Optional[str] = None
    serials: List[str] = field(default_factory=list)
    usb_serials: Dict[str, str] = field(default_factory=dict)

# ─── Helpers ────────────────────────────────────────────────────────────────

def parse_ip_config(path):
    chs = []
    with open(path) as f:
        for ln, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith('#'): continue
            p = [x.strip() for x in line.split(',')]
            if len(p) != 4: continue
            try: chs.append(DevCh(int(p[0]), int(p[1]), p[2], int(p[3])))
            except ValueError: pass
    return chs

def unique_devs(chs):
    devs = {}
    for c in chs:
        if c.ip not in devs: devs[c.ip] = DevInfo(ip=c.ip)
        devs[c.ip].channels.append(c)
    return devs

def classify(name):
    if PAT_CONFIG.match(name): return 'config'
    if PAT_TEST.match(name): return 'test'
    if PAT_LOG.match(name): return 'log'
    if PAT_APP.match(name): return 'firmware'
    if PAT_EMPTY.match(name): return 'empty'
    if PAT_DATA.match(name): return 'data'
    if PAT_ANY_BIN.match(name): return 'data'
    return 'other'


def parse_bin_date(filename):
    """Parse YYMMDDHH from a .BIN filename. Returns datetime or None."""
    base = os.path.basename(filename).upper().replace('.BIN', '')
    # Match 8-digit date-hour pattern
    m = re.match(r'^(\d{8})$', base)
    if not m:
        return None
    try:
        s = m.group(1)
        return datetime(2000 + int(s[0:2]), int(s[2:4]), int(s[4:6]), int(s[6:8]))
    except ValueError:
        return None


def extract_serial(ftp, usb_dir):
    """Grab a data file from a USB dir, find status block, extract serial."""
    try:
        ftp.cwd('/' + usb_dir)
        files = sorted([f for f in ftp.nlst()
                        if len(f) == 12 and f[:8].isdigit() and f.upper().endswith('.BIN')])
        if not files:
            return None
        # Grab first ~30KB (header + a few data records)
        buf = bytearray()
        def grab(d):
            if len(buf) < 30000: buf.extend(d)
        ftp.retrbinary(f'RETR {files[-1]}', grab, blocksize=8192, rest=0)
        if len(buf) < 14080 + 600 * 4:
            return None
        # Scan data record for the status block by finding counter break
        # Status block: 12 words where counter sequence breaks
        data = buf[14080:]  # skip file header
        for i in range(4, len(data) - 52, 4):
            prev_ctr = data[i - 4]
            this_ctr = data[i]
            diff = (this_ctr - prev_ctr) & 0xFF
            if diff != 1:
                # Found status block start. Serial is 10 words in (index +10)
                serial_off = i + 10 * 4
                if serial_off + 4 > len(data):
                    break
                raw = data[serial_off:serial_off + 4]
                serial = f'{raw[3]:02X}{raw[2]:02X}{raw[1]:02X}{raw[0]:02X}'
                if serial != '00000000' and serial != 'FFFFFFFF':
                    return serial
                break
        return None
    except Exception:
        return None

def ftp_conn(ip):
    ftp = ftplib.FTP()
    ftp.connect(ip, CFG["ftp_port"], timeout=CFG["ftp_timeout"])
    ftp.login(CFG["ftp_user"], CFG["ftp_pass"])
    return ftp

def probe(dev):
    """Connect to device FTP, scan USB1-USB4 directories for files."""
    try:
        ftp = ftp_conn(dev.ip)
        dev.online = True

        # List root to find USB directories
        ftp.cwd('/')
        root_entries = []
        ftp.dir(root_entries.append)

        # Find all top-level directories (USB1, USB2, etc.)
        dirs = []
        for e in root_entries:
            p = e.split()
            if p and e.startswith('d') and p[-1] not in ('.', '..'):
                dirs.append('/' + p[-1])

        # Also check root itself for any files
        dirs.insert(0, '/')

        # Extract serial numbers for each USB directory
        usb_serials = {}
        for d in dirs:
            dirname = d.strip('/')
            if dirname.startswith('USB'):
                serial = extract_serial(ftp, dirname)
                if serial:
                    usb_serials[dirname] = serial
                    logging.debug(f'{dev.ip}: {dirname} -> {serial}')
        dev.usb_serials = usb_serials

        found = []
        for d in dirs:
            try:
                ftp.cwd(d)
                entries = []
                ftp.dir(entries.append)
                for e in entries:
                    p = e.split()
                    if not p or e.startswith('d'): continue
                    nm = p[-1]
                    # Accept .BIN and .TXT files
                    if PAT_ANY_BIN.match(nm) or PAT_LOG.match(nm):
                        try: sz = ftp.size(nm) or 0
                        except: sz = 0
                        remote_path = f"{d}/{nm}" if d != '/' else f"/{nm}"
                        found.append(RFile(remote_path, sz, classify(nm)))
            except ftplib.error_perm:
                pass

        dev.files = found
        ftp.quit()
    except Exception as e:
        dev.online = False
        dev.error = str(e)
    return dev

def download_file(ip, rpath, lpath, prog_cb=None):
    try:
        os.makedirs(os.path.dirname(lpath), exist_ok=True)
        ftp = ftp_conn(ip)
        try: tot = ftp.size(rpath) or 0
        except: tot = 0
        # Resume partial download if file exists
        rest = 0
        if os.path.exists(lpath) and tot > 0:
            existing = os.path.getsize(lpath)
            if 0 < existing < tot:
                rest = existing
        got = rest; t0 = time.time()
        mode = 'ab' if rest > 0 else 'wb'
        with open(lpath, mode) as f:
            def cb(data):
                nonlocal got; f.write(data); got += len(data)
                if prog_cb and tot: prog_cb(got, tot, t0)
            ftp.retrbinary(f'RETR {rpath}', cb, blocksize=32768, rest=rest if rest else None)
        ftp.quit()
        el = time.time() - t0; dl = got - rest; spd = dl/el if el else 0
        resumed = f" (resumed from {rest/(1<<20):.1f} MB)" if rest else ""
        return True, f"OK ({got:,} B, {spd/1024:.0f} KB/s{resumed})"
    except Exception as e:
        # Keep partial file for resume — don't delete
        return False, str(e)

def bar(frac, w=25):
    f = int(w*frac)
    return f"[{'█'*f}{'░'*(w-f)}] {frac*100:5.1f}%"

def show_prog(got, tot, t0):
    el = time.time()-t0; spd = got/el if el else 0
    s = f"{spd/1024:.0f} KB/s" if spd < 1<<20 else f"{spd/(1<<20):.1f} MB/s"
    sys.stdout.write(f"\r  {bar(got/tot if tot else 0)} {s}  "); sys.stdout.flush()

# ─── TCP Control Listener (background) ─────────────────────────────────────

class HeartbeatListener:
    """Background TCP listener on port 8810 for node heartbeats.
    Nodes connect to this port to register themselves."""

    def __init__(self, port=None):
        self.port = port or CFG["tcp_port"]
        self.known_ips = set()
        self._stop = threading.Event()
        self._thread = None
        self._srv = None

    def start(self):
        self._thread = threading.Thread(target=self._listen, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._srv:
            try: self._srv.close()
            except: pass

    def _listen(self):
        try:
            self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._srv.bind(('0.0.0.0', self.port))
            self._srv.listen(20)
            self._srv.settimeout(1.0)

            while not self._stop.is_set():
                try:
                    conn, addr = self._srv.accept()
                    self.known_ips.add(addr[0])
                    conn.settimeout(2)
                    try: conn.recv(1024)  # Read and discard heartbeat
                    except: pass
                    conn.close()
                except socket.timeout:
                    pass
        except OSError as e:
            logging.debug(f"Heartbeat listener error: {e}")
        finally:
            if self._srv:
                try: self._srv.close()
                except: pass

# ─── Commands ───────────────────────────────────────────────────────────────

def cmd_scan(devs, par=4):
    print(f"\n{C.B}Scanning {len(devs)} devices...{C.RST}\n")
    with ThreadPoolExecutor(max_workers=par) as pool:
        futs = {pool.submit(probe, d): ip for ip, d in devs.items()}
        for fut in as_completed(futs):
            ip = futs[fut]; d = fut.result(); devs[ip] = d
            if d.online:
                df = [f for f in d.files if f.ftype == 'data']
                cl = ', '.join(c.label for c in d.channels)
                total = sum(f.size for f in df)
                mb = total / (1<<20)
                gb = total / (1<<30)
                sz = f"{gb:.1f} GB" if gb >= 1 else f"{mb:.0f} MB"
                serials = ', '.join(f'{k}={v}' for k,v in d.usb_serials.items()) if d.usb_serials else ''
                serial_str = f"  [{serials}]" if serials else ''
                print(f"  {C.G}●{C.RST} {ip:16s}  {cl:20s}  "
                      f"{len(df):3d} data files  ({sz}){serial_str}")
            else:
                print(f"  {C.R}○{C.RST} {ip:16s}  OFFLINE  {C.D}{d.error}{C.RST}")
    on = sum(1 for d in devs.values() if d.online)
    nf = sum(len(d.files) for d in devs.values() if d.online)
    n_data = sum(1 for d in devs.values() if d.online for f in d.files if f.ftype == 'data')
    n_empty = sum(1 for d in devs.values() if d.online for f in d.files if f.ftype == 'empty')
    total = sum(f.size for d in devs.values() if d.online for f in d.files if f.ftype != 'empty')
    gb = total/(1<<30)
    empty_note = f", {n_empty} empty skipped" if n_empty else ""
    print(f"\n{C.B}{on}/{len(devs)} online, {n_data} data files ({gb:.1f} GB){empty_note}{C.RST}")
    return devs

def cmd_list(devs):
    for ip, d in sorted(devs.items()):
        if not d.online: continue
        cl = ', '.join(c.label for c in d.channels)
        total = sum(f.size for f in d.files)
        serials = ', '.join(f'{k}→{v}' for k,v in d.usb_serials.items()) if d.usb_serials else ''
        serial_note = f"  [{serials}]" if serials else ''
        print(f"\n{C.B}{C.CY}{ip}{C.RST} ({cl}) — {total/(1<<30):.1f} GB{serial_note}")
        for ft in ('config', 'test', 'log', 'firmware', 'data', 'empty', 'other'):
            ff = [f for f in d.files if f.ftype == ft]
            if not ff: continue
            grp_sz = sum(f.size for f in ff)
            print(f"  {C.Y}{ft.upper()}{C.RST} ({len(ff)} files, {grp_sz/(1<<20):.0f} MB):")
            if ft == 'empty':
                print(f"    ({len(ff)} pre-allocated empty files — skipped)")
                continue
            # Show first/last 5 if many files
            show = ff if len(ff) <= 12 else ff[:5] + [None] + ff[-5:]
            for f in sorted(show, key=lambda x: x.name if x else ''):
                if f is None:
                    print(f"    {'...':30s}  ({len(ff)-10} more files)")
                else:
                    print(f"    {os.path.basename(f.name):30s}  {f.size_h:>10s}")

def cmd_dl(devs, outdir, ftypes=None, ips=None, skip=True, date_from=None, date_to=None):
    if ftypes is None: ftypes = ['data']
    outdir = os.path.abspath(outdir); os.makedirs(outdir, exist_ok=True)
    q = []
    for ip, d in sorted(devs.items()):
        if not d.online or (ips and ip not in ips): continue
        for f in d.files:
            if f.ftype == 'empty': continue  # Always skip pre-allocated empty files
            if f.ftype not in ftypes: continue
            if date_from or date_to:
                fdate = parse_bin_date(f.name)
                if fdate is None: continue  # Skip non-date files when date filter active
                if date_from and fdate < date_from: continue
                if date_to and fdate > date_to: continue
            # Build local path: outdir/IP/serial_or_USB/filename
            parts = f.name.strip('/').split('/')
            if len(parts) >= 2:
                subdir = parts[0]  # e.g. "USB1"
                # Use serial number as dir name if available
                serial = d.usb_serials.get(subdir, subdir)
                fname = parts[-1]
                lp = os.path.join(outdir, ip.replace('.','_'), serial, fname)
            else:
                fname = parts[0]
                lp = os.path.join(outdir, ip.replace('.','_'), fname)
            if skip and os.path.exists(lp) and os.path.getsize(lp) == f.size: continue
            q.append((ip, f.name, lp, f))
    if not q:
        print(f"{C.Y}Nothing to download (all files up-to-date or no matching files).{C.RST}"); return
    total = sum(f.size for _,_,_,f in q)
    gb = total/(1<<30)
    print(f"\n{C.B}Downloading {len(q)} files ({gb:.1f} GB) → {outdir}{C.RST}\n")
    ok = fail = 0; dl_bytes = 0; dl_start = time.time()
    for i, (ip, rp, lp, f) in enumerate(q, 1):
        fn = os.path.basename(rp)
        usb = rp.strip('/').split('/')[0] if '/' in rp.strip('/') else ''
        prefix = f"{usb}/{fn}" if usb else fn
        print(f"  {C.BL}↓{C.RST} [{i}/{len(q)}] {ip} / {prefix} ({f.size_h})")
        s, msg = download_file(ip, rp, lp, show_prog)
        sys.stdout.write('\r'+' '*70+'\r')
        if s:
            print(f"  {C.G}✓{C.RST} {prefix} — {msg}")
            ok += 1; dl_bytes += f.size
        else:
            print(f"  {C.R}✗{C.RST} {prefix} — {msg}")
            fail += 1
    elapsed = time.time() - dl_start
    avg_speed = dl_bytes/elapsed if elapsed else 0
    print(f"\n{C.B}Done: {ok} downloaded ({dl_bytes/(1<<30):.1f} GB), {fail} failed, "
          f"{avg_speed/(1<<20):.1f} MB/s avg{C.RST}")

def cmd_interactive(devs, outdir):
    # Start heartbeat listener in background
    hb = HeartbeatListener()
    try:
        hb.start()
        print(f"{C.D}TCP listener on :{CFG['tcp_port']} for node heartbeats{C.RST}")
    except:
        pass

    while True:
        print(f"\n{C.B}╔══════════════════════════════════════════╗{C.RST}")
        print(f"{C.B}║   Allseis Seismic Data Downloader v2     ║{C.RST}")
        print(f"{C.B}╚══════════════════════════════════════════╝{C.RST}")
        on = sum(1 for d in devs.values() if d.online)
        sc = any(d.online for d in devs.values())
        print(f"  {'%d/%d online'%(on,len(devs)) if sc else '%d configured'%len(devs)}")
        if hb.known_ips:
            print(f"  {C.G}Heartbeats from: {', '.join(sorted(hb.known_ips))}{C.RST}")
        print(f"\n  {C.CY}1{C.RST}  Scan devices       {C.CY}5{C.RST}  Download test data")
        print(f"  {C.CY}2{C.RST}  List files          {C.CY}6{C.RST}  Download config files")
        print(f"  {C.CY}3{C.RST}  Download ALL data   {C.CY}7{C.RST}  Set output directory")
        print(f"  {C.CY}4{C.RST}  Download by IP      {C.CY}q{C.RST}  Quit")
        print(f"  {C.D}Output: {outdir}{C.RST}")
        ch = input(f"\n  {C.B}>{C.RST} ").strip().lower()
        if ch == '1': devs = cmd_scan(devs)
        elif ch == '2':
            if sc: cmd_list(devs)
            else: print(f"{C.Y}Scan first.{C.RST}")
        elif ch == '3':
            if sc: cmd_dl(devs, outdir)  # Interactive: no date filter by default
            else: print(f"{C.Y}Scan first.{C.RST}")
        elif ch == '4':
            if not sc: print(f"{C.Y}Scan first.{C.RST}"); continue
            oips = [ip for ip,d in devs.items() if d.online]
            for i,ip in enumerate(oips,1):
                n=len([f for f in devs[ip].files if f.ftype=='data'])
                gb=sum(f.size for f in devs[ip].files if f.ftype=='data')/(1<<30)
                print(f"  {C.CY}{i}{C.RST}  {ip} ({n} files, {gb:.1f} GB)")
            s=input(f"  Device # or IP: ").strip()
            try: t=oips[int(s)-1]
            except: t=s
            if t in devs: cmd_dl(devs, outdir, ips=[t])
            else: print(f"{C.R}Not found.{C.RST}")
        elif ch == '5' and sc: cmd_dl(devs, outdir, ftypes=['test'])
        elif ch == '6' and sc: cmd_dl(devs, outdir, ftypes=['config'])
        elif ch == '7':
            d=input("  Dir: ").strip()
            if d: outdir=os.path.abspath(d); print(f"  → {outdir}")
        elif ch in ('q','quit','exit'):
            hb.stop(); print(f"{C.D}Bye!{C.RST}"); break

def cmd_csv(devs, path):
    with open(path,'w',newline='') as f:
        w=csv.writer(f)
        w.writerow(['IP','Online','Channels','USBDir','FileName','Type','Size','Path'])
        for ip,d in sorted(devs.items()):
            c=';'.join(x.label for x in d.channels)
            if not d.files: w.writerow([ip,d.online,c,'','','','',''])
            for fi in d.files:
                parts = fi.name.strip('/').split('/')
                usb = parts[0] if len(parts) >= 2 else ''
                w.writerow([ip,d.online,c,usb,os.path.basename(fi.name),
                            fi.ftype,fi.size,fi.name])
    print(f"Inventory → {path}")

# ─── Main ───────────────────────────────────────────────────────────────────

def find_cfg():
    for n in ('ip.config','Ip.config'):
        for b in ('.',os.path.dirname(os.path.abspath(__file__))):
            p=os.path.join(b,n)
            if os.path.exists(p): return p

def main():
    ap = argparse.ArgumentParser(description='Allseis Seismic Data Downloader v2',
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog="""
Examples:
  %(prog)s                                    Interactive mode
  %(prog)s --scan                             Scan devices
  %(prog)s --download-all -o ./data           Download everything
  %(prog)s --manual-ip 192.168.1.110 --list   List files on one device""")
    ap.add_argument('--config','-c', help='ip.config path')
    ap.add_argument('--output','-o', default='./seismic_data', help='Output directory')
    ap.add_argument('--scan', action='store_true')
    ap.add_argument('--list', action='store_true')
    ap.add_argument('--download-all', action='store_true')
    ap.add_argument('--download-test', action='store_true')
    ap.add_argument('--download-config', action='store_true')
    ap.add_argument('--ip', nargs='+', help='Filter by IP(s)')
    ap.add_argument('--parallel','-j', type=int, default=4)
    ap.add_argument('--from-date', default=None,
                    help='Download from date (YYMMDD or YYMMDDHH, e.g. 231228 or 23122805)')
    ap.add_argument('--to-date', default=None,
                    help='Download to date (YYMMDD or YYMMDDHH, e.g. 240101 or 24010123)')
    ap.add_argument('--no-skip', action='store_true', help='Re-download existing')
    ap.add_argument('--export-csv', help='Export inventory CSV')
    ap.add_argument('--ftp-user', default='pi')
    ap.add_argument('--ftp-pass', default='raspberry')
    ap.add_argument('--manual-ip', nargs='+', help='IPs without ip.config')
    ap.add_argument('--verbose','-v', action='store_true')
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING)
    CFG["ftp_user"]=args.ftp_user; CFG["ftp_pass"]=args.ftp_pass

    if args.manual_ip:
        chs=[DevCh(1,i,ip,1) for i,ip in enumerate(args.manual_ip,1)]
    else:
        cp = args.config or find_cfg()
        if not cp:
            print(f"{C.R}No ip.config found. Use --config or --manual-ip{C.RST}"); sys.exit(1)
        print(f"{C.D}Config: {cp}{C.RST}")
        chs = parse_ip_config(cp)
    if not chs: print(f"{C.R}No channels.{C.RST}"); sys.exit(1)

    devs = unique_devs(chs)
    print(f"{C.D}{len(chs)} channels, {len(devs)} devices{C.RST}")


    # Parse date filters
    date_from = date_to = None
    if args.from_date:
        s = args.from_date
        try:
            if len(s) == 6: date_from = datetime(2000+int(s[0:2]), int(s[2:4]), int(s[4:6]))
            elif len(s) == 8: date_from = datetime(2000+int(s[0:2]), int(s[2:4]), int(s[4:6]), int(s[6:8]))
            else: print(f"{C.R}Bad --from-date: use YYMMDD or YYMMDDHH{C.RST}"); sys.exit(1)
            print(f"{C.D}Date from: {date_from}{C.RST}")
        except ValueError: print(f"{C.R}Bad --from-date: {s}{C.RST}"); sys.exit(1)
    if args.to_date:
        s = args.to_date
        try:
            if len(s) == 6: date_to = datetime(2000+int(s[0:2]), int(s[2:4]), int(s[4:6]), 23)
            elif len(s) == 8: date_to = datetime(2000+int(s[0:2]), int(s[2:4]), int(s[4:6]), int(s[6:8]))
            else: print(f"{C.R}Bad --to-date: use YYMMDD or YYMMDDHH{C.RST}"); sys.exit(1)
            print(f"{C.D}Date to:   {date_to}{C.RST}")
        except ValueError: print(f"{C.R}Bad --to-date: {s}{C.RST}"); sys.exit(1)
    any_cmd = args.scan or args.list or args.download_all or args.download_test \
              or args.download_config or args.export_csv
    if any_cmd:
        devs = cmd_scan(devs, args.parallel)
        if args.list: cmd_list(devs)
        if args.export_csv: cmd_csv(devs, args.export_csv)
        sk = not args.no_skip
        if args.download_all: cmd_dl(devs, args.output, ['data'], args.ip, sk, date_from, date_to)
        if args.download_test: cmd_dl(devs, args.output, ['test'], args.ip, sk, date_from, date_to)
        if args.download_config: cmd_dl(devs, args.output, ['config'], args.ip, sk, date_from, date_to)
    else:
        cmd_interactive(devs, os.path.abspath(args.output))

if __name__ == '__main__':
    main()
