/*
 * ExternalGameSync Pre-Launcher — fullscreen styled dialog with controller support
 *
 * All dimensions scale with screen resolution so it's legible at 4K.
 * Controller input via XInput (loaded dynamically — no SDK needed).
 *
 * Build (mingw-w64 cross-compile from Linux — requires stb_image.h in same dir):
 *   x86_64-w64-mingw32-gcc -O2 -mwindows -Wall -static-libgcc -o pre-launcher.exe pre-launcher.c -lgdi32 -lmsimg32
 *
 * Build (MSVC Developer PowerShell):
 *   cl /O2 /W3 pre-launcher.c user32.lib gdi32.lib msimg32.lib /Fe:pre-launcher.exe /link /subsystem:windows
 *
 * IPC files (all inside %TEMP%):
 *   egs_status.txt     - STATUS / GAME / GAME_EXE written by wrapper.sh before launch
 *   egs_ready.txt      - written by Linux handler when sync phase is complete
 *   egs_choice.txt     - written here after user picks conflict resolution
 *   egs_cancelled.txt  - written here when user cancels launch
 *   egs_push_start.txt - written here after game exits to trigger Linux-side push
 *   egs_push_done.txt  - written by Linux handler when push is complete
 */

#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <tlhelp32.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <stdarg.h>
#include "icon_data.h"

/* ── Diagnostic log (%TEMP%\egs_diag.txt, readable from Linux via IPC dir) ── */

static FILE *g_diag = NULL;

static void diag_open(void)
{
    char path[MAX_PATH * 2];

    /* First choice: next to the exe — guaranteed writable since we installed it there */
    char exe_path[MAX_PATH] = {0};
    if (GetModuleFileNameA(NULL, exe_path, MAX_PATH)) {
        char *sep = NULL;
        for (char *p = exe_path; *p; p++) if (*p == '\\' || *p == '/') sep = p;
        if (sep) {
            *(sep + 1) = 0;
            snprintf(path, sizeof(path), "%segs_diag.txt", exe_path);
            g_diag = fopen(path, "w");
        }
    }

    /* Second choice: %TEMP% */
    if (!g_diag) {
        char tmp[MAX_PATH];
        ExpandEnvironmentStringsA("%TEMP%", tmp, MAX_PATH);
        snprintf(path, sizeof(path), "%s\\egs_diag.txt", tmp);
        g_diag = fopen(path, "w");
    }

    if (g_diag) {
        fprintf(g_diag, "=== ExternalGameSync Pre-Launcher Diagnostics ===\n");
        fprintf(g_diag, "diag file: %s\n", path);
        fprintf(g_diag, "exe path:  %s\n", exe_path);
        fflush(g_diag);
    }
}

static void diag(const char *fmt, ...)
{
    if (!g_diag) return;
    va_list ap; va_start(ap, fmt);
    vfprintf(g_diag, fmt, ap);
    va_end(ap);
    fputc('\n', g_diag);
    fflush(g_diag);
}

static void diag_env(const char *name)
{
    char val[1024] = {0};
    if (GetEnvironmentVariableA(name, val, sizeof(val)))
        diag("  env %s=%s", name, val);
    else
        diag("  env %s=(not set, err=%lu)", name, GetLastError());
}

static void diag_drives(void)
{
    DWORD mask = GetLogicalDrives();
    diag("logical drives mask: 0x%lx", mask);
    for (int i = 0; i < 26; i++) {
        if (!(mask & (1u << i))) continue;
        char root[4] = { (char)('A' + i), ':', '\\', 0 };
        UINT type = GetDriveTypeA(root);
        const char *tname;
        switch (type) {
        case DRIVE_REMOVABLE:    tname = "removable";  break;
        case DRIVE_FIXED:        tname = "fixed";       break;
        case DRIVE_REMOTE:       tname = "remote";      break;
        case DRIVE_CDROM:        tname = "cdrom";       break;
        case DRIVE_RAMDISK:      tname = "ramdisk";     break;
        case DRIVE_NO_ROOT_DIR:  tname = "no_root_dir"; break;
        default:                 tname = "unknown";     break;
        }
        char vol[256] = {0}, fs[64] = {0};
        GetVolumeInformationA(root, vol, sizeof(vol), NULL, NULL, NULL, fs, sizeof(fs));
        diag("  drive %s type=%-12s label=\"%s\" fs=%s", root, tname, vol, fs);
    }
}

static void diag_exe(const char *exe)
{
    if (!exe || !exe[0]) { diag("game_exe: (empty)"); return; }
    diag("game_exe: %s", exe);
    DWORD attr = GetFileAttributesA(exe);
    if (attr == INVALID_FILE_ATTRIBUTES) {
        diag("  -> NOT FOUND (GetFileAttributes error=%lu)", GetLastError());
    } else {
        diag("  -> exists, attr=0x%lx%s", attr,
             (attr & FILE_ATTRIBUTE_DIRECTORY) ? " [DIR]" : " [file]");
    }
    /* Also try the parent directory */
    char dir[MAX_PATH * 2];
    strncpy(dir, exe, sizeof(dir) - 1);
    dir[sizeof(dir) - 1] = 0;
    char *sep = NULL;
    for (char *p = dir; *p; p++) if (*p == '\\' || *p == '/') sep = p;
    if (sep) {
        *sep = 0;
        DWORD da = GetFileAttributesA(dir);
        diag("  parent dir %s: %s (attr=0x%lx)",
             dir,
             da == INVALID_FILE_ATTRIBUTES ? "NOT FOUND" : "exists",
             da);
    }
}

/* ── Runtime scaling (all sizes in "base 1080p pixels", scaled at startup) ─ */

static int  g_sw = 1920, g_sh = 1080;  /* window/screen dimensions used for rendering */
static int  g_scale_n = 1, g_scale_d = 1; /* scale as integer fraction n/d */
static BOOL g_windowed = FALSE; /* TRUE: small centered dialog; FALSE: fullscreen */

/* Scale a base-1080p value */
static int S(int base) { return base * g_scale_n / g_scale_d; }

/* Scale a base-1080p font point size */
static int SF(int pt) { return S(pt); }

/* Fullscreen only in Steam Gaming Mode (gamescope/SteamOS).
 * Native Windows (no STEAM_COMPAT_DATA_PATH) and Linux desktop both get the
 * small windowed dialog. */
static BOOL is_gaming_mode(void)
{
    char compat[MAX_PATH] = {0};
    if (!GetEnvironmentVariableA("STEAM_COMPAT_DATA_PATH", compat, sizeof(compat)) || !compat[0])
        return FALSE; /* native Windows — always windowed */
    char val[16] = {0};
    GetEnvironmentVariableA("STEAM_GAMEPADUI", val, sizeof(val));
    return val[0] != '\0' && val[0] != '0';
}

static void init_scale(void)
{
    g_windowed = !is_gaming_mode();
    if (g_windowed) {
        g_sw = 800; g_sh = 600;
        g_scale_n = 1; g_scale_d = 1;
    } else {
        g_sw = GetSystemMetrics(SM_CXSCREEN);
        g_sh = GetSystemMetrics(SM_CYSCREEN);
        /* Use height to drive scale; maintain integer ratio for crisp fonts */
        g_scale_n = g_sh;
        g_scale_d = 1080;
    }
}

/* ── Colours ─────────────────────────────────────────────────────────────── */

#define C_BG      RGB(0x0e,0x0e,0x1a)
#define C_PANEL   RGB(0x16,0x16,0x26)
#define C_ACCENT  RGB(0x40,0x40,0xd0)
#define C_DIM     RGB(0x70,0x70,0x90)
#define C_TEXT    RGB(0xd8,0xd8,0xe8)
#define C_WHITE   RGB(0xff,0xff,0xff)
#define C_GREEN   RGB(0x18,0x80,0x48)
#define C_RED     RGB(0xa8,0x24,0x24)
#define C_GOLD    RGB(0xa0,0x7c,0x00)
#define C_BLUE    RGB(0x1c,0x54,0xb0)
#define C_SEL_BDR RGB(0x60,0x60,0xff)

/* ── XInput ──────────────────────────────────────────────────────────────── */

#define XI_LEFT  0x0004
#define XI_RIGHT 0x0008
#define XI_START 0x0010
#define XI_A     0x1000
#define XI_B     0x2000
#define XI_Y     0x8000

typedef struct {
    DWORD pkt;
    struct { WORD btn; BYTE lt, rt; SHORT lx, ly, rx, ry; } pad;
} XISTATE;
typedef DWORD (WINAPI *PFN_XI)(DWORD, XISTATE *);

static PFN_XI g_xi      = NULL;
static WORD   g_xi_prev = 0;

static void xi_init(void)
{
    const char *dlls[] = {"xinput1_4.dll","xinput1_3.dll","xinput9_1_0.dll",NULL};
    for (int i = 0; dlls[i]; i++) {
        HMODULE h = LoadLibraryA(dlls[i]);
        if (h) { g_xi = (PFN_XI)GetProcAddress(h, "XInputGetState"); if (g_xi) break; }
    }
}

static WORD xi_poll(void)
{
    if (!g_xi) return 0;
    XISTATE s = {0}; WORD cur = 0;
    for (DWORD i = 0; i < 4; i++) if (g_xi(i, &s) == 0) cur |= s.pad.btn;
    WORD p = cur & ~g_xi_prev; g_xi_prev = cur; return p;
}

/* ── Icon (stb_image — no GDI+ needed, works in Wine) ───────────────────── */

#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wunused-function"
#pragma GCC diagnostic ignored "-Wsign-compare"
#pragma GCC diagnostic ignored "-Wmaybe-uninitialized"
#define STB_IMAGE_IMPLEMENTATION
#define STBI_ONLY_PNG
#define STBI_NO_STDIO
#include "stb_image.h"
#pragma GCC diagnostic pop

static HBITMAP g_icon_bmp = NULL;
static int     g_icon_w   = 0, g_icon_h = 0;

static void icon_load(void)
{
    int w, h, ch;
    unsigned char *px = stbi_load_from_memory(icon_png, (int)icon_png_len,
                                               &w, &h, &ch, 4);
    if (!px) return;

    BITMAPINFO bi = {0};
    bi.bmiHeader.biSize        = sizeof(BITMAPINFOHEADER);
    bi.bmiHeader.biWidth       = w;
    bi.bmiHeader.biHeight      = -h; /* top-down */
    bi.bmiHeader.biPlanes      = 1;
    bi.bmiHeader.biBitCount    = 32;
    bi.bmiHeader.biCompression = BI_RGB;

    void *bits = NULL;
    HDC dc = GetDC(NULL);
    g_icon_bmp = CreateDIBSection(dc, &bi, DIB_RGB_COLORS, &bits, NULL, 0);
    ReleaseDC(NULL, dc);

    if (g_icon_bmp && bits) {
        /* Convert RGBA (stb_image) to pre-multiplied BGRA required by AlphaBlend */
        const unsigned char *s = px;
        unsigned char *d = (unsigned char *)bits;
        for (int i = 0; i < w * h; i++, s += 4, d += 4) {
            unsigned a = s[3];
            d[0] = (unsigned char)(s[2] * a / 255); /* B */
            d[1] = (unsigned char)(s[1] * a / 255); /* G */
            d[2] = (unsigned char)(s[0] * a / 255); /* R */
            d[3] = (unsigned char)a;
        }
        g_icon_w = w; g_icon_h = h;
    }

    stbi_image_free(px);
}

static void icon_free(void)
{
    if (g_icon_bmp) { DeleteObject(g_icon_bmp); g_icon_bmp = NULL; }
    g_icon_w = g_icon_h = 0;
}

/* ── IPC helpers ─────────────────────────────────────────────────────────── */

#define MAX_VAL   4096
#define MAX_PATH2 (MAX_PATH * 2)

typedef struct { char status[64]; char game[512]; char game_exe[MAX_VAL]; char last_status[64]; } EgsStatus;

/* File that ready_exists() / wait_ready() polls — set before each run_dialog call */
static char g_ready_poll[MAX_PATH2] = {0};

static void ipc_path(const char *name, char *out, DWORD sz)
{
    /* Use USERPROFILE\AppData\Local\Temp — matches the path the bash wrapper
     * hardcodes via pfx/drive_c/users/steamuser/AppData/Local/Temp.
     * Wine's %TEMP% resolves to the shorter C:\users\steamuser\Temp which
     * is a different directory. */
    char profile[MAX_PATH] = {0};
    if (GetEnvironmentVariableA("USERPROFILE", profile, MAX_PATH) && profile[0]) {
        snprintf(out, sz, "%s\\AppData\\Local\\Temp\\%s", profile, name);
    } else {
        /* Fallback for non-Wine environments */
        char tmp[MAX_PATH];
        ExpandEnvironmentStringsA("%TEMP%", tmp, MAX_PATH);
        snprintf(out, sz, "%s\\%s", tmp, name);
    }
}

static void set_poll_file(const char *name)
{
    ipc_path(name, g_ready_poll, sizeof(g_ready_poll));
}

static void delete_poll_file(void)
{
    if (g_ready_poll[0]) DeleteFileA(g_ready_poll);
    g_ready_poll[0] = 0;
}

static void read_status(EgsStatus *s)
{
    char path[MAX_PATH2]; ipc_path("egs_status.txt", path, sizeof(path));
    FILE *f = fopen(path, "r"); if (!f) return;
    char line[MAX_VAL + 64];
    while (fgets(line, sizeof(line), f)) {
        line[strcspn(line,"\r\n")] = 0;
        char *eq = strchr(line,'='); if (!eq) continue; *eq = 0;
        char *k = line, *v = eq + 1;
        if (!strcmp(k,"STATUS"))      strncpy(s->status,     v,sizeof(s->status)     -1);
        if (!strcmp(k,"GAME"))        strncpy(s->game,       v,sizeof(s->game)       -1);
        if (!strcmp(k,"GAME_EXE"))    strncpy(s->game_exe,   v,sizeof(s->game_exe)   -1);
        if (!strcmp(k,"LAST_STATUS")) strncpy(s->last_status,v,sizeof(s->last_status)-1);
    }
    fclose(f);
}

static void write_choice(const char *c)
{
    char path[MAX_PATH2]; ipc_path("egs_choice.txt", path, sizeof(path));
    FILE *f = fopen(path,"w"); if (f) { fputs(c,f); fclose(f); }
}

static void write_cancel_file(void)
{
    char path[MAX_PATH2]; ipc_path("egs_cancelled.txt", path, sizeof(path));
    FILE *f = fopen(path,"w"); if (f) { fclose(f); }
}

static void write_push_signal(void)
{
    char path[MAX_PATH2]; ipc_path("egs_push_start.txt", path, sizeof(path));
    FILE *f = fopen(path,"w"); if (f) { fputs("push",f); fclose(f); }
}

static int ready_exists(void)
{
    if (!g_ready_poll[0]) return 0;
    return GetFileAttributesA(g_ready_poll) != INVALID_FILE_ATTRIBUTES;
}

/* Returns TRUE if any running process has its exe inside dir (prefix match). */
static BOOL any_process_in_dir(const char *dir)
{
    size_t dirlen = strlen(dir);
    HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
    if (snap == INVALID_HANDLE_VALUE) return FALSE;

    PROCESSENTRY32 pe = {sizeof(pe)};
    BOOL found = FALSE;
    if (Process32First(snap, &pe)) {
        do {
            if (pe.th32ProcessID <= 4) continue; /* skip idle/system */
            HANDLE ph = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, pe.th32ProcessID);
            if (!ph) continue;
            char path[MAX_PATH2] = {0};
            DWORD sz = sizeof(path);
            if (QueryFullProcessImageNameA(ph, 0, path, &sz) &&
                _strnicmp(path, dir, dirlen) == 0 &&
                (path[dirlen] == '\\' || path[dirlen] == '\0'))
                found = TRUE;
            CloseHandle(ph);
        } while (!found && Process32Next(snap, &pe));
    }
    CloseHandle(snap);
    return found;
}

static int run_wait(const char *exe)
{
    char cmd[MAX_VAL+4]; snprintf(cmd,sizeof(cmd),"\"%s\"",exe);
    diag("run_wait: cmd=%s", cmd);

    /* Set working dir to the exe's own directory so relative-path launchers work */
    char work_dir[MAX_VAL];
    strncpy(work_dir, exe, sizeof(work_dir)-1);
    work_dir[sizeof(work_dir)-1] = 0;
    char *last_sep = NULL;
    for (char *p = work_dir; *p; p++)
        if (*p == '\\' || *p == '/') last_sep = p;
    if (last_sep) *last_sep = 0;
    else work_dir[0] = 0;
    diag("run_wait: work_dir=%s", work_dir[0] ? work_dir : "(none)");

    /* Create a job object + IOCP so we're notified when all job-tracked
     * processes exit. Launch suspended so the process is in the job before
     * it can spawn children. */
    HANDLE job  = CreateJobObjectA(NULL, NULL);
    HANDLE iocp = job ? CreateIoCompletionPort(INVALID_HANDLE_VALUE, NULL, 0, 1) : NULL;
    if (job && iocp) {
        JOBOBJECT_ASSOCIATE_COMPLETION_PORT jacp = {0};
        jacp.CompletionKey  = job;
        jacp.CompletionPort = iocp;
        SetInformationJobObject(job, JobObjectAssociateCompletionPortInformation,
                                &jacp, sizeof(jacp));
    }
    diag("run_wait: job=%p iocp=%p", job, iocp);

    STARTUPINFOA si={sizeof(si)}; PROCESS_INFORMATION pi={0};
    DWORD flags = NORMAL_PRIORITY_CLASS | (job ? CREATE_SUSPENDED : 0);
    if (!CreateProcessA(NULL, cmd, NULL, NULL, FALSE, flags,
                        NULL, work_dir[0] ? work_dir : NULL, &si, &pi)) {
        diag("run_wait: CreateProcess FAILED err=%lu", GetLastError());
        if (iocp) CloseHandle(iocp);
        if (job)  CloseHandle(job);
        return -1;
    }
    diag("run_wait: CreateProcess OK pid=%lu", pi.dwProcessId);
    if (job) { AssignProcessToJobObject(job, pi.hProcess); ResumeThread(pi.hThread); }

    /* Wait for the launcher process itself to exit */
    WaitForSingleObject(pi.hProcess, INFINITE);
    DWORD code=0; GetExitCodeProcess(pi.hProcess, &code);
    CloseHandle(pi.hProcess); CloseHandle(pi.hThread);

    /* Wait for the job to drain (catches children that properly inherit the job).
     * 4-hour timeout in case a child hangs; loop discards unrelated messages. */
    if (job && iocp) {
        DWORD      msg; ULONG_PTR key; LPOVERLAPPED ov;
        int ticks = 86400 * 2; /* 24-hour max */
        while (ticks-- > 0) {
            if (!GetQueuedCompletionStatus(iocp, &msg, &key, &ov, 500)) continue;
            if ((HANDLE)key == job && msg == JOB_OBJECT_MSG_ACTIVE_PROCESS_ZERO) break;
        }
        CloseHandle(iocp);
        CloseHandle(job);
    }

    /* Directory scan: catches processes that broke away from the job (e.g. RE2's
     * launcher spawning the game as a fully independent process). Wait until no
     * process whose exe lives under work_dir has been seen for 1 second. */
    if (work_dir[0]) {
        int clear = 0, ticks = 14400 * 2;
        while (clear < 2 && ticks-- > 0) {
            Sleep(500);
            clear = any_process_in_dir(work_dir) ? 0 : clear + 1;
        }
    }

    return (int)code;
}

/* ── Dialog ──────────────────────────────────────────────────────────────── */

#define TIMER_XI      1
#define TIMER_READY   2
#define TIMER_DISMISS 3

typedef struct { char label[80]; COLORREF color; int result; RECT r; } Btn;

static Btn  g_btns[3];
static int  g_nbtns = 0, g_sel = 0, g_done = -1;
static BOOL g_syncing = FALSE;
static char g_heading[128], g_body[512];

/* ── Phase state machine ─────────────────────────────────────────────────── */

typedef enum {
    PH_SYNCING_PRE,     /* waiting for pre-launch pull */
    PH_CONFLICT,        /* conflict choice — interactive */
    PH_SYNCING_RESOLVE, /* waiting for conflict-resolution pull */
    PH_NO_CONNECTION,   /* no-connection choice — interactive */
    PH_SYNCED_OK,       /* brief "Saves synced!" notification */
    PH_PUSHING,         /* waiting for post-game push */
    PH_PUSHED,          /* brief "Saves uploaded!" notification */
} Phase;

static Phase g_phase;

/* Returns TRUE for phases where any button press closes the window early */
static BOOL phase_dismissable(void)
{
    return g_phase == PH_SYNCED_OK || g_phase == PH_PUSHED;
}

/* ── GDI helpers ─────────────────────────────────────────────────────────── */

static HFONT make_font(int base_pt, BOOL bold)
{
    return CreateFontA(
        -SF(base_pt), 0, 0, 0,
        bold ? FW_BOLD : FW_NORMAL,
        FALSE, FALSE, FALSE, DEFAULT_CHARSET,
        OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS, CLEARTYPE_QUALITY,
        DEFAULT_PITCH|FF_SWISS, "Segoe UI");
}

static void fill(HDC dc, int x, int y, int w, int h, COLORREF c)
{
    RECT r = {x,y,x+w,y+h}; HBRUSH b = CreateSolidBrush(c);
    FillRect(dc,&r,b); DeleteObject(b);
}

static void rrect(HDC dc, RECT *r, COLORREF fc, COLORREF bc, int bw)
{
    int rx = S(10)*2;
    HBRUSH b = CreateSolidBrush(fc); HPEN p = CreatePen(PS_SOLID,bw,bc);
    HBRUSH ob=(HBRUSH)SelectObject(dc,b); HPEN op=(HPEN)SelectObject(dc,p);
    RoundRect(dc,r->left,r->top,r->right,r->bottom,rx,rx);
    SelectObject(dc,ob); SelectObject(dc,op); DeleteObject(b); DeleteObject(p);
}

/* ── Paint ───────────────────────────────────────────────────────────────── */

static void paint(HWND hwnd)
{
    PAINTSTRUCT ps; HDC hdc = BeginPaint(hwnd, &ps);
    HDC mem = CreateCompatibleDC(hdc);
    HBITMAP bmp = CreateCompatibleBitmap(hdc, g_sw, g_sh);
    HBITMAP obmp = (HBITMAP)SelectObject(mem, bmp);

    int pad     = S(48);
    int btn_h   = S(72);
    int btn_gap = S(16);
    int acc_h   = S(4);

    /* full-screen background */
    fill(mem, 0, 0, g_sw, g_sh, C_BG);

    /* windowed: panel fills the whole window; fullscreen: centered 70%×60% panel */
    int pw = g_windowed ? g_sw : g_sw * 7 / 10;
    int ph = g_windowed ? g_sh : g_sh * 6 / 10;
    int px = (g_sw - pw) / 2;
    int py = (g_sh - ph) / 2;

    RECT panel = {px, py, px+pw, py+ph};
    HBRUSH panelbr = CreateSolidBrush(C_PANEL);
    FillRect(mem, &panel, panelbr);
    DeleteObject(panelbr);

    /* accent bar at top of panel */
    fill(mem, px, py, pw, acc_h, C_ACCENT);

    /* centered logo — drawn behind all text/buttons */
    if (g_icon_bmp) {
        int icon_sz = (pw < ph ? pw : ph) / 2;
        int ix = px + (pw - icon_sz) / 2;
        int iy = py + (ph - icon_sz) / 2;
        HDC icon_dc = CreateCompatibleDC(mem);
        HBITMAP old_bmp = (HBITMAP)SelectObject(icon_dc, g_icon_bmp);
        BLENDFUNCTION bf = {AC_SRC_OVER, 0, 255, AC_SRC_ALPHA};
        AlphaBlend(mem, ix, iy, icon_sz, icon_sz,
                   icon_dc, 0, 0, g_icon_w, g_icon_h, bf);
        SelectObject(icon_dc, old_bmp);
        DeleteDC(icon_dc);
    }

    SetBkMode(mem, TRANSPARENT);

    /* "EXTERNALGAMESYNC" label */
    {
        HFONT f = make_font(13, TRUE), o = (HFONT)SelectObject(mem, f);
        SetTextColor(mem, C_DIM);
        RECT r = {px+pad, py+S(12), px+pw-pad, py+S(40)};
        DrawTextA(mem, "EXTERNALGAMESYNC", -1, &r, DT_LEFT|DT_VCENTER|DT_SINGLELINE);
        DeleteObject(SelectObject(mem, o));
    }

    /* heading */
    {
        HFONT f = make_font(26, TRUE), o = (HFONT)SelectObject(mem, f);
        SetTextColor(mem, C_WHITE);
        RECT r = {px+pad, py+S(44), px+pw-pad, py+S(100)};
        DrawTextA(mem, g_heading, -1, &r, DT_LEFT|DT_VCENTER|DT_SINGLELINE);
        DeleteObject(SelectObject(mem, o));
    }

    /* divider */
    {
        HPEN p = CreatePen(PS_SOLID,S(1),RGB(0x28,0x28,0x44));
        HPEN op = (HPEN)SelectObject(mem, p);
        int dy = py + S(106);
        MoveToEx(mem, px+pad, dy, NULL); LineTo(mem, px+pw-pad, dy);
        SelectObject(mem, op); DeleteObject(p);
    }

    /* body text */
    {
        HFONT f = make_font(18, FALSE), o = (HFONT)SelectObject(mem, f);
        SetTextColor(mem, C_TEXT);
        int bottom = g_syncing
            ? py + ph - pad
            : py + ph - btn_h - pad*2 - S(8);
        RECT r = {px+pad, py+S(114), px+pw-pad, bottom};
        DrawTextA(mem, g_body, -1, &r, DT_LEFT|DT_WORDBREAK);
        DeleteObject(SelectObject(mem, o));
    }

    /* buttons */
    if (!g_syncing) {
        /* layout buttons inside the panel */
        int bw = (pw - pad*2 - btn_gap*(g_nbtns-1)) / g_nbtns;
        int by = py + ph - btn_h - pad;
        for (int i = 0; i < g_nbtns; i++) {
            g_btns[i].r.left   = px + pad + i*(bw+btn_gap);
            g_btns[i].r.right  = g_btns[i].r.left + bw;
            g_btns[i].r.top    = by;
            g_btns[i].r.bottom = by + btn_h;
        }

        HFONT f = make_font(18, TRUE), o = (HFONT)SelectObject(mem, f);
        for (int i = 0; i < g_nbtns; i++) {
            BOOL sel = (i == g_sel);
            rrect(mem, &g_btns[i].r, g_btns[i].color,
                  sel ? C_SEL_BDR : g_btns[i].color, sel ? S(3) : S(1));
            SetTextColor(mem, C_WHITE);
            DrawTextA(mem, g_btns[i].label, -1, &g_btns[i].r,
                      DT_CENTER|DT_VCENTER|DT_SINGLELINE);
        }
        DeleteObject(SelectObject(mem, o));

        /* navigation hint below panel */
        {
            HFONT f2 = make_font(14, FALSE), o2 = (HFONT)SelectObject(mem, f2);
            SetTextColor(mem, C_DIM);
            RECT r = {px, py+ph+S(12), px+pw, py+ph+S(36)};
            DrawTextA(mem,
                "Press labeled button directly   \xb7   D-Pad / Arrows to navigate   \xb7   Start / Enter to confirm   \xb7   Esc to cancel",
                -1, &r, DT_CENTER|DT_SINGLELINE);
            DeleteObject(SelectObject(mem, o2));
        }
    }

    BitBlt(hdc, 0, 0, g_sw, g_sh, mem, 0, 0, SRCCOPY);
    SelectObject(mem, obmp); DeleteObject(bmp); DeleteDC(mem);
    EndPaint(hwnd, &ps);
}

/* ── Window proc ─────────────────────────────────────────────────────────── */

static void set_sel(HWND hwnd, int idx)
{
    g_sel = (idx + g_nbtns) % g_nbtns;
    InvalidateRect(hwnd, NULL, FALSE);
}

/* Forward declarations for setup functions used by advance() */
static void setup_conflict(const char *game);
static void setup_syncing(void);
static void setup_synced(void);
static void setup_pushed(void);

/* Advance the phase state machine.  Called by timers and interactive choices.
 * For passive phases (syncing, pushed notification) this transitions the
 * window content in-place.  For terminal states it calls PostQuitMessage. */
static void advance(HWND hwnd, int result)
{
    switch (g_phase) {

    case PH_SYNCING_PRE: {
        /* Pull complete — re-read status and decide what to show next */
        EgsStatus ns = {0};
        delete_poll_file();
        read_status(&ns);
        KillTimer(hwnd, TIMER_READY);
        if (!strcmp(ns.status, "conflict")) {
            g_phase = PH_CONFLICT;
            setup_conflict(ns.game);
        } else {
            g_phase = PH_SYNCED_OK;
            setup_synced();
            SetTimer(hwnd, TIMER_DISMISS, 2000, NULL);
        }
        InvalidateRect(hwnd, NULL, FALSE);
        return;
    }

    case PH_CONFLICT:
        if (result == IDCANCEL || result < 0) {
            write_cancel_file();
            g_done = IDCANCEL;
            PostQuitMessage(0);
            return;
        }
        write_choice(result == IDYES ? "remote" : "local");
        g_phase = PH_SYNCING_RESOLVE;
        set_poll_file("egs_ready.txt");
        setup_syncing();
        SetTimer(hwnd, TIMER_READY, 500, NULL);
        InvalidateRect(hwnd, NULL, FALSE);
        return;

    case PH_SYNCING_RESOLVE:
        delete_poll_file();
        KillTimer(hwnd, TIMER_READY);
        g_phase = PH_SYNCED_OK;
        setup_synced();
        SetTimer(hwnd, TIMER_DISMISS, 2000, NULL);
        InvalidateRect(hwnd, NULL, FALSE);
        return;

    case PH_NO_CONNECTION:
        if (result == IDNO || result < 0) {
            write_cancel_file();
            g_done = IDCANCEL;
        } else {
            g_done = IDYES;
        }
        PostQuitMessage(0);
        return;

    case PH_SYNCED_OK:
        g_done = IDYES;
        PostQuitMessage(0);
        return;

    case PH_PUSHING:
        delete_poll_file();
        KillTimer(hwnd, TIMER_READY);
        g_phase = PH_PUSHED;
        setup_pushed();
        SetTimer(hwnd, TIMER_DISMISS, 3000, NULL);
        InvalidateRect(hwnd, NULL, FALSE);
        return;

    case PH_PUSHED:
        g_done = 0;
        PostQuitMessage(0);
        return;
    }
}

static LRESULT CALLBACK wnd_proc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp)
{
    switch (msg) {
    case WM_PAINT:        paint(hwnd); return 0;
    case WM_ERASEBKGND:   return 1;

    case WM_TIMER:
        if (wp == TIMER_XI) {
            WORD p = xi_poll();
            if (!g_syncing) {
                if (p & XI_LEFT)                  set_sel(hwnd, g_sel-1);
                if (p & XI_RIGHT)                 set_sel(hwnd, g_sel+1);
                if (p & XI_A)                     advance(hwnd, g_btns[0].result);
                if ((p & XI_B) && g_nbtns > 1)    advance(hwnd, g_btns[1].result);
                if ((p & XI_Y) && g_nbtns > 2)    advance(hwnd, g_btns[2].result);
                if (p & XI_START)                 advance(hwnd, g_btns[g_sel].result);
            } else if (phase_dismissable()) {
                if (p & (XI_A|XI_B|XI_Y|XI_START)) advance(hwnd, 0);
            }
        } else if (wp == TIMER_READY) {
            if (ready_exists()) advance(hwnd, 0);
        } else if (wp == TIMER_DISMISS) {
            advance(hwnd, 0);
        }
        return 0;

    case WM_KEYDOWN:
        if (!g_syncing) {
            switch (wp) {
            case VK_LEFT:  case VK_UP:    set_sel(hwnd, g_sel-1); break;
            case VK_RIGHT: case VK_DOWN:  set_sel(hwnd, g_sel+1); break;
            case VK_RETURN: case VK_SPACE: advance(hwnd, g_btns[g_sel].result); break;
            case VK_ESCAPE:
                advance(hwnd, g_nbtns > 2 ? g_btns[2].result : g_btns[g_nbtns-1].result);
                break;
            }
        } else if (phase_dismissable()) {
            if (wp == VK_ESCAPE || wp == VK_RETURN || wp == VK_SPACE) advance(hwnd, 0);
        }
        return 0;

    case WM_LBUTTONDOWN: {
        if (g_syncing) {
            if (phase_dismissable()) advance(hwnd, 0);
            return 0;
        }
        int x = (short)LOWORD(lp), y = (short)HIWORD(lp);
        for (int i = 0; i < g_nbtns; i++)
            if (x >= g_btns[i].r.left && x < g_btns[i].r.right &&
                y >= g_btns[i].r.top  && y < g_btns[i].r.bottom)
                advance(hwnd, g_btns[i].result);
        return 0;
    }

    case WM_MOUSEMOVE: {
        if (g_syncing) return 0;
        int x = (short)LOWORD(lp), y = (short)HIWORD(lp);
        for (int i = 0; i < g_nbtns; i++)
            if (x >= g_btns[i].r.left && x < g_btns[i].r.right &&
                y >= g_btns[i].r.top  && y < g_btns[i].r.bottom)
                { set_sel(hwnd, i); break; }
        return 0;
    }

    case WM_DESTROY: if (g_done < 0) PostQuitMessage(0); return 0;
    }
    return DefWindowProcA(hwnd, msg, wp, lp);
}

/* ── Dialog content setup ────────────────────────────────────────────────── */

static void setup_conflict(const char *game)
{
    snprintf(g_heading, sizeof(g_heading), "%s - Save Conflict", game[0] ? game : "Game");
    snprintf(g_body, sizeof(g_body),
        "Saves have changed on cloud storage since your last sync.\n\n"
        "Both sides will be backed up before any changes are made.");
    g_nbtns = 3; g_syncing = FALSE;
    strncpy(g_btns[0].label,"(A)  Keep Cloud",    79); g_btns[0].color=C_GREEN;  g_btns[0].result=IDYES;
    strncpy(g_btns[1].label,"(B)  Keep Local",    79); g_btns[1].color=C_RED;    g_btns[1].result=IDNO;
    strncpy(g_btns[2].label,"(Y)  Cancel Launch", 79); g_btns[2].color=C_GOLD;   g_btns[2].result=IDCANCEL;
}

static void setup_no_connection(const char *game)
{
    snprintf(g_heading, sizeof(g_heading), "%s - No Connection", game[0] ? game : "Game");
    snprintf(g_body, sizeof(g_body),
        "Could not reach cloud storage before launching.\n\n"
        "Your local saves are safe but won't be updated from the server.\n"
        "Continue launching anyway?");
    g_nbtns = 2; g_syncing = FALSE;
    strncpy(g_btns[0].label,"(A)  Continue Anyway",79); g_btns[0].color=C_BLUE; g_btns[0].result=IDYES;
    strncpy(g_btns[1].label,"(B)  Cancel Launch",  79); g_btns[1].color=C_RED;  g_btns[1].result=IDNO;
}

static void setup_no_connection_server_ahead(const char *game)
{
    snprintf(g_heading, sizeof(g_heading), "%s - No Connection", game[0] ? game : "Game");
    snprintf(g_body, sizeof(g_body),
        "Could not reach cloud storage before launching.\n\n"
        "WARNING: The server had unsynced changes last time it was reachable.\n"
        "You may be playing with outdated saves.\n\n"
        "Continue launching anyway?");
    g_nbtns = 2; g_syncing = FALSE;
    strncpy(g_btns[0].label,"(A)  Continue Anyway",79); g_btns[0].color=C_BLUE; g_btns[0].result=IDYES;
    strncpy(g_btns[1].label,"(B)  Cancel Launch",  79); g_btns[1].color=C_RED;  g_btns[1].result=IDNO;
}

static void setup_syncing(void)
{
    strncpy(g_heading, "Syncing saves...", sizeof(g_heading)-1);
    strncpy(g_body,
        "Syncing your saves with cloud storage.\n\n"
        "The game will launch automatically when the sync completes.",
        sizeof(g_body)-1);
    g_nbtns = 0; g_syncing = TRUE;
}

static void setup_synced(void)
{
    strncpy(g_heading, "Saves synced!", sizeof(g_heading)-1);
    strncpy(g_body, "Saves are up to date. Launching game...", sizeof(g_body)-1);
    g_nbtns = 0; g_syncing = TRUE;
}

static void setup_pushing(void)
{
    strncpy(g_heading, "Saving game...", sizeof(g_heading)-1);
    strncpy(g_body, "Pushing your saves to cloud storage.", sizeof(g_body)-1);
    g_nbtns = 0; g_syncing = TRUE;
}

static void setup_pushed(void)
{
    strncpy(g_heading, "Saves uploaded!", sizeof(g_heading)-1);
    strncpy(g_body, "See you next time.", sizeof(g_body)-1);
    g_nbtns = 0; g_syncing = TRUE;
}

/* ── Create / run a dialog window, return g_done ────────────────────────── */

static int run_dialog(HINSTANCE hInst, BOOL ready_timer)
{
    static BOOL registered = FALSE;
    if (!registered) {
        WNDCLASSA wc = {0};
        wc.lpfnWndProc   = wnd_proc;
        wc.hInstance     = hInst;
        wc.lpszClassName = "EGSDialog";
        wc.hCursor       = LoadCursorA(NULL, IDC_ARROW);
        RegisterClassA(&wc);
        registered = TRUE;
    }

    g_done = -1;

    int wx = 0, wy = 0;
    if (g_windowed) {
        wx = (GetSystemMetrics(SM_CXSCREEN) - g_sw) / 2;
        wy = (GetSystemMetrics(SM_CYSCREEN) - g_sh) / 2;
    }
    HWND hwnd = CreateWindowExA(
        WS_EX_TOPMOST | WS_EX_APPWINDOW,
        "EGSDialog", "ExternalGameSync",
        WS_POPUP | WS_VISIBLE,
        wx, wy, g_sw, g_sh,
        NULL, NULL, hInst, NULL);
    diag("run_dialog: CreateWindowExA hwnd=%p err=%lu screen=%dx%d phase=%d",
         hwnd, GetLastError(), g_sw, g_sh, (int)g_phase);
    if (!hwnd) return -1;

    RECT wr = {0}; GetWindowRect(hwnd, &wr);
    diag("run_dialog: window rect (%ld,%ld)-(%ld,%ld)", wr.left, wr.top, wr.right, wr.bottom);
    HMONITOR hm = MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST);
    MONITORINFO mi = {sizeof(mi)};
    if (GetMonitorInfoA(hm, &mi))
        diag("run_dialog: monitor work=(%ld,%ld)-(%ld,%ld) full=(%ld,%ld)-(%ld,%ld) primary=%s",
             mi.rcWork.left, mi.rcWork.top, mi.rcWork.right, mi.rcWork.bottom,
             mi.rcMonitor.left, mi.rcMonitor.top, mi.rcMonitor.right, mi.rcMonitor.bottom,
             (mi.dwFlags & MONITORINFOF_PRIMARY) ? "yes" : "no");

    SetTimer(hwnd, TIMER_XI, 33, NULL);
    if (ready_timer) SetTimer(hwnd, TIMER_READY, 500, NULL);
    /* Auto-dismiss when starting directly at a notification phase */
    if      (g_phase == PH_SYNCED_OK) SetTimer(hwnd, TIMER_DISMISS, 2000, NULL);
    else if (g_phase == PH_PUSHED)    SetTimer(hwnd, TIMER_DISMISS, 3000, NULL);
    SetForegroundWindow(hwnd);

    MSG msg;
    while (GetMessageA(&msg, NULL, 0, 0)) { TranslateMessage(&msg); DispatchMessageA(&msg); }
    DestroyWindow(hwnd);
    return g_done;
}

/* ── Pre-game sequence (one window for all pre-launch phases) ────────────── */

static int run_pre_game(HINSTANCE hInst, const EgsStatus *s0)
{
    EgsStatus s;
    memcpy(&s, s0, sizeof s);
    BOOL was_syncing = !strcmp(s.status, "syncing");

    /* Fast path: pull already finished before pre-launcher.exe started.
     * Check unconditionally — Linux may have updated egs_status.txt from
     * "syncing" to "in_sync" before we read it, so was_syncing can be FALSE
     * even though a sync did complete. */
    char ready_path[MAX_PATH2];
    ipc_path("egs_ready.txt", ready_path, sizeof ready_path);
    if (GetFileAttributesA(ready_path) != INVALID_FILE_ATTRIBUTES) {
        DeleteFileA(ready_path);
        memset(&s, 0, sizeof s);
        read_status(&s);
        was_syncing = TRUE; /* sync definitely completed */
    }

    if (!strcmp(s.status, "syncing")) {
        /* Normal sync: window starts at syncing, advance() handles the rest */
        g_phase = PH_SYNCING_PRE;
        set_poll_file("egs_ready.txt");
        setup_syncing();
        run_dialog(hInst, /*ready_timer=*/TRUE);
    } else if (!strcmp(s.status, "conflict")) {
        /* Conflict without a prior syncing phase */
        g_phase = PH_CONFLICT;
        setup_conflict(s.game);
        run_dialog(hInst, FALSE);
    } else if (!strcmp(s.status, "no_connection")) {
        int sa = (!strcmp(s.last_status, "cloud_ahead") || !strcmp(s.last_status, "conflict"));
        g_phase = PH_NO_CONNECTION;
        if (sa) setup_no_connection_server_ahead(s.game);
        else    setup_no_connection(s.game);
        run_dialog(hInst, FALSE);
    } else if (was_syncing) {
        /* Clean fast-pull: show brief "Saves synced!" notification */
        g_phase = PH_SYNCED_OK;
        setup_synced();
        run_dialog(hInst, FALSE); /* run_dialog sets 2s timer for PH_SYNCED_OK */
    } else {
        return IDYES; /* nothing to show */
    }

    return (g_done == IDCANCEL) ? IDCANCEL : IDYES;
}

/* ── Post-game sequence (one window: pushing → pushed) ───────────────────── */

static void run_post_game(HINSTANCE hInst)
{
    write_push_signal();
    g_phase = PH_PUSHING;
    set_poll_file("egs_push_done.txt");
    setup_pushing();
    run_dialog(hInst, /*ready_timer=*/TRUE);
    /* advance() handles PH_PUSHING → PH_PUSHED → PostQuitMessage */
}

/* ── Entry point ─────────────────────────────────────────────────────────── */

int WINAPI WinMain(HINSTANCE hInst, HINSTANCE hPrev, LPSTR lpCmd, int nShow)
{
    (void)hPrev; (void)lpCmd; (void)nShow;

    diag_open();
    init_scale();
    xi_init();
    icon_load();

    diag("=== startup ===");
    diag("screen: %dx%d  scale: %d/%d", g_sw, g_sh, g_scale_n, g_scale_d);
    diag("icon_bmp: %p  %dx%d", g_icon_bmp, g_icon_w, g_icon_h);
    diag("xi: %p", g_xi);

    diag("--- environment ---");
    diag_env("STEAM_COMPAT_DATA_PATH");
    diag_env("STEAM_COMPAT_CLIENT_INSTALL_PATH");
    diag_env("SteamAppId");
    diag_env("TEMP");
    diag_env("TMP");
    diag_env("SystemRoot");
    diag_env("WINEPREFIX");
    diag_env("WINE_MONO_VERSION");

    diag("--- drives ---");
    diag_drives();

    EgsStatus s = {0};
    read_status(&s);
    diag("--- status file ---");
    diag("STATUS=%s", s.status);
    diag("GAME=%s",   s.game);
    diag("LAST_STATUS=%s", s.last_status);

    diag("--- game exe ---");
    diag_exe(s.game_exe);

    diag("--- pre-game sequence ---");
    if (run_pre_game(hInst, &s) == IDCANCEL) {
        diag("pre_game: CANCELLED");
        icon_free();
        return 1;
    }
    diag("pre_game: OK  g_done=%d", g_done);

    if (!s.game_exe[0]) {
        diag("no game_exe — exiting cleanly");
        icon_free();
        return 0;
    }

    diag("--- launching game ---");
    int exit_code = run_wait(s.game_exe);
    diag("run_wait returned: %d", exit_code);

    run_post_game(hInst);
    diag("post_game done");

    icon_free();
    return exit_code;
}
