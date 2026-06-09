/*
 * ExternalGameSync Pre-Launcher — SDL2/SDL_ttf, cross-platform (Linux + Windows/Wine)
 *
 * Linux build:
 *   gcc -O2 -Wall -o pre-launcher pre-launcher.c \
 *       $(sdl2-config --cflags --libs) -lSDL2_ttf
 *
 * Windows cross-compile (from Linux, requires mingw-w64 SDL2/SDL2_ttf packages):
 *   x86_64-w64-mingw32-gcc -O2 -mwindows -Wall -static-libgcc \
 *       -o pre-launcher.exe pre-launcher.c \
 *       $(pkg-config --cflags --libs sdl2 SDL2_ttf | sed 's/-I/-I/g') \
 *       -DSDL_MAIN_HANDLED
 *
 * Linux invocation (by the bash wrapper):
 *   pre-launcher pre   → pre-game phases; exits 0=ok, 1=cancelled
 *   pre-launcher post  → post-game phases (write push signal, show pushing/pushed)
 *
 * Windows invocation (splice into Proton %command%):
 *   pre-launcher.exe   → full flow: pre-game, run_wait(game_exe), post-game
 *
 * IPC files (all in IPC_DIR):
 *   egs_status.txt     written by wrapper.sh before launch
 *   egs_ready.txt      written by sync handler when pre-launch pull is done
 *   egs_choice.txt     written here after user resolves conflict
 *   egs_cancelled.txt  written here when user cancels
 *   egs_push_start.txt written here to trigger post-game push
 *   egs_push_done.txt  written by sync handler when push is done
 */

#ifdef _WIN32
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <tlhelp32.h>
#define SDL_MAIN_HANDLED
#endif

#include <SDL2/SDL.h>
#include <SDL2/SDL_ttf.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <stdarg.h>
#ifndef _WIN32
#include <sys/stat.h>
#endif

#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wunused-function"
#pragma GCC diagnostic ignored "-Wsign-compare"
#pragma GCC diagnostic ignored "-Wmaybe-uninitialized"
#define STB_IMAGE_IMPLEMENTATION
#define STBI_ONLY_PNG
#define STBI_NO_STDIO
#include "stb_image.h"
#pragma GCC diagnostic pop

#include "icon_data.h"
#include "bg_data.h"

/* ── Diagnostic log ────────────────────────────────────────────────────────── */

static FILE *g_diag = NULL;

static void diag_open(void)
{
    char path[4096];
#ifdef _WIN32
    char exe_path[MAX_PATH] = {0};
    if (GetModuleFileNameA(NULL, exe_path, MAX_PATH)) {
        char *sep = strrchr(exe_path, '\\');
        if (!sep) sep = strrchr(exe_path, '/');
        if (sep) { *(sep+1) = 0; snprintf(path, sizeof(path), "%segs_diag.txt", exe_path); g_diag = fopen(path, "w"); }
    }
    if (!g_diag) {
        char tmp[MAX_PATH]; ExpandEnvironmentStringsA("%TEMP%", tmp, MAX_PATH);
        snprintf(path, sizeof(path), "%s\\egs_diag.txt", tmp); g_diag = fopen(path, "w");
    }
#else
    snprintf(path, sizeof(path), "/tmp/externalgamesync/egs_diag.txt");
    g_diag = fopen(path, "w");
#endif
    if (g_diag) { fprintf(g_diag, "=== ExternalGameSync Pre-Launcher (SDL2) ===\n"); fflush(g_diag); }
}

static void diag(const char *fmt, ...)
{
    if (!g_diag) return;
    va_list ap; va_start(ap, fmt); vfprintf(g_diag, fmt, ap); va_end(ap);
    fputc('\n', g_diag); fflush(g_diag);
}

/* ── IPC paths ─────────────────────────────────────────────────────────────── */

static char g_ipc_dir[4096] = {0};

static void ipc_init(void)
{
#ifdef _WIN32
    char profile[MAX_PATH] = {0};
    if (GetEnvironmentVariableA("USERPROFILE", profile, MAX_PATH) && profile[0])
        snprintf(g_ipc_dir, sizeof(g_ipc_dir), "%s\\AppData\\Local\\Temp", profile);
    else {
        char tmp[MAX_PATH]; ExpandEnvironmentStringsA("%TEMP%", tmp, MAX_PATH);
        snprintf(g_ipc_dir, sizeof(g_ipc_dir), "%s", tmp);
    }
#else
    const char *compat = getenv("STEAM_COMPAT_DATA_PATH");
    if (compat && compat[0])
        snprintf(g_ipc_dir, sizeof(g_ipc_dir),
                 "%s/pfx/drive_c/users/steamuser/AppData/Local/Temp", compat);
    else {
        snprintf(g_ipc_dir, sizeof(g_ipc_dir), "/tmp/externalgamesync");
        mkdir(g_ipc_dir, 0700);
    }
#endif
    diag("ipc_dir: %s", g_ipc_dir);
}

static void ipc_path(const char *name, char *out, size_t sz)
{
#ifdef _WIN32
    snprintf(out, sz, "%s\\%s", g_ipc_dir, name);
#else
    snprintf(out, sz, "%s/%s", g_ipc_dir, name);
#endif
}

static int file_exists(const char *path)
{
    FILE *f = fopen(path, "r");
    if (!f) return 0;
    fclose(f); return 1;
}

static void file_write(const char *path, const char *content)
{
    FILE *f = fopen(path, "w");
    if (f) { fputs(content, f); fclose(f); }
}

static void file_delete(const char *path)
{
#ifdef _WIN32
    DeleteFileA(path);
#else
    remove(path);
#endif
}

/* ── Status file ───────────────────────────────────────────────────────────── */

typedef struct {
    char status[64];
    char game[512];
    char game_exe[4096];
    char last_status[64];
    int  fullscreen;
} EgsStatus;

static void read_status(EgsStatus *s)
{
    char path[4096]; ipc_path("egs_status.txt", path, sizeof(path));
    FILE *f = fopen(path, "r"); if (!f) return;
    char line[4160];
    while (fgets(line, sizeof(line), f)) {
        line[strcspn(line, "\r\n")] = 0;
        char *eq = strchr(line, '='); if (!eq) continue; *eq = 0;
        char *k = line, *v = eq + 1;
        if (!strcmp(k, "STATUS"))      strncpy(s->status,      v, sizeof(s->status)      - 1);
        if (!strcmp(k, "GAME"))        strncpy(s->game,        v, sizeof(s->game)        - 1);
        if (!strcmp(k, "GAME_EXE"))    strncpy(s->game_exe,    v, sizeof(s->game_exe)    - 1);
        if (!strcmp(k, "LAST_STATUS")) strncpy(s->last_status, v, sizeof(s->last_status) - 1);
        if (!strcmp(k, "FULLSCREEN"))  s->fullscreen = (v[0] == '1');
    }
    fclose(f);
}

static void write_choice(const char *c)
{
    char path[4096]; ipc_path("egs_choice.txt", path, sizeof(path));
    file_write(path, c);
}

static void write_cancel(void)
{
    char path[4096]; ipc_path("egs_cancelled.txt", path, sizeof(path));
    file_write(path, "");
}

static void write_push_signal(void)
{
    char path[4096]; ipc_path("egs_push_start.txt", path, sizeof(path));
    file_write(path, "push");
}

/* Poll file used by the ready-check timer */
static char g_poll_file[4096] = {0};

static void set_poll_file(const char *name)
{
    ipc_path(name, g_poll_file, sizeof(g_poll_file));
}

static void clear_poll_file(void)
{
    if (g_poll_file[0]) { file_delete(g_poll_file); g_poll_file[0] = 0; }
}

static int poll_file_exists(void) { return g_poll_file[0] && file_exists(g_poll_file); }

/* ── Scaling ───────────────────────────────────────────────────────────────── */

static int g_sw = 800, g_sh = 600;
static int g_scale_n = 1, g_scale_d = 1;
static int g_windowed = 1;

static int S(int base) { return base * g_scale_n / g_scale_d; }

static void init_scale(int fullscreen_hint)
{
    g_windowed = !fullscreen_hint;
    if (g_windowed) {
        g_sw = 800; g_sh = 600;
        g_scale_n = 1; g_scale_d = 1;
    } else {
        SDL_DisplayMode dm;
        if (SDL_GetCurrentDisplayMode(0, &dm) == 0) { g_sw = dm.w; g_sh = dm.h; }
        else { g_sw = 1920; g_sh = 1080; }
        g_scale_n = g_sh; g_scale_d = 1080;
    }
    diag("scale: %dx%d  n/d=%d/%d  windowed=%d", g_sw, g_sh, g_scale_n, g_scale_d, g_windowed);
}

/* ── Colours ───────────────────────────────────────────────────────────────── */

#define COL(r,g,b) ((SDL_Color){r,g,b,255})
#define COLA(r,g,b,a) ((SDL_Color){r,g,b,a})

static const SDL_Color C_BG      = COL(0x0e,0x0e,0x1a);
static const SDL_Color C_PANEL   = COL(0x16,0x16,0x26);
static const SDL_Color C_ACCENT  = COL(0x40,0x40,0xd0);
static const SDL_Color C_DIV     = COL(0x28,0x28,0x44);
static const SDL_Color C_DIM     = COL(0x70,0x70,0x90);
static const SDL_Color C_TEXT    = COL(0xd8,0xd8,0xe8);
static const SDL_Color C_WHITE   = COL(0xff,0xff,0xff);
static const SDL_Color C_GREEN   = COL(0x18,0x80,0x48);
static const SDL_Color C_RED     = COL(0xa8,0x24,0x24);
static const SDL_Color C_GOLD    = COL(0xa0,0x7c,0x00);
static const SDL_Color C_BLUE    = COL(0x1c,0x54,0xb0);
static const SDL_Color C_SEL_BDR = COL(0x60,0x60,0xff);

/* ── SDL renderer globals ──────────────────────────────────────────────────── */

static SDL_Window   *g_win  = NULL;
static SDL_Renderer *g_ren  = NULL;
static SDL_Texture  *g_icon = NULL;
static int           g_icon_w = 0, g_icon_h = 0;

static SDL_Texture  *g_bg_pulling      = NULL;
static SDL_Texture  *g_bg_pushing      = NULL;
static SDL_Texture  *g_bg_done         = NULL;
static SDL_Texture  *g_bg_conflict     = NULL;
static SDL_Texture  *g_bg_disconnected = NULL;

/* ── Fonts ─────────────────────────────────────────────────────────────────── */

static TTF_Font *g_fnt_heading = NULL; /* 26pt bold  */
static TTF_Font *g_fnt_body    = NULL; /* 18pt       */
static TTF_Font *g_fnt_label   = NULL; /* 13pt bold  */
static TTF_Font *g_fnt_hint    = NULL; /* 14pt       */
static TTF_Font *g_fnt_btn     = NULL; /* 18pt bold  */

static const char *font_search_paths[] = {
#ifdef _WIN32
    "C:\\Windows\\Fonts\\segoeui.ttf",
    "C:\\Windows\\Fonts\\arial.ttf",
#else
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/opentype/noto/NotoSans-Regular.ttf",
    "/usr/share/fonts/noto/NotoSans-Regular.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    "/usr/share/fonts/gnu-free/FreeSans.ttf",
#endif
    NULL
};
static const char *font_search_paths_bold[] = {
#ifdef _WIN32
    "C:\\Windows\\Fonts\\segoeuib.ttf",
    "C:\\Windows\\Fonts\\arialbd.ttf",
#else
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/opentype/noto/NotoSans-Bold.ttf",
    "/usr/share/fonts/noto/NotoSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    "/usr/share/fonts/gnu-free/FreeSansBold.ttf",
#endif
    NULL
};

static const char *find_font(int bold)
{
    const char **paths = bold ? font_search_paths_bold : font_search_paths;
    for (int i = 0; paths[i]; i++)
        if (file_exists(paths[i])) return paths[i];
    /* fallback: try the other weight */
    const char **alt = bold ? font_search_paths : font_search_paths_bold;
    for (int i = 0; alt[i]; i++)
        if (file_exists(alt[i])) return alt[i];
    return NULL;
}

static int fonts_load(void)
{
    const char *reg  = find_font(0);
    const char *bold = find_font(1);
    if (!reg && !bold) { diag("no font found"); return 0; }
    if (!bold) bold = reg;
    if (!reg)  reg  = bold;
    diag("font regular: %s", reg);
    diag("font bold:    %s", bold);
    g_fnt_heading = TTF_OpenFont(bold, S(26));
    g_fnt_body    = TTF_OpenFont(reg,  S(18));
    g_fnt_label   = TTF_OpenFont(bold, S(13));
    g_fnt_hint    = TTF_OpenFont(reg,  S(14));
    g_fnt_btn     = TTF_OpenFont(bold, S(18));
    return g_fnt_heading && g_fnt_body && g_fnt_btn;
}

static void fonts_free(void)
{
    if (g_fnt_heading) { TTF_CloseFont(g_fnt_heading); g_fnt_heading = NULL; }
    if (g_fnt_body)    { TTF_CloseFont(g_fnt_body);    g_fnt_body    = NULL; }
    if (g_fnt_label)   { TTF_CloseFont(g_fnt_label);   g_fnt_label   = NULL; }
    if (g_fnt_hint)    { TTF_CloseFont(g_fnt_hint);    g_fnt_hint    = NULL; }
    if (g_fnt_btn)     { TTF_CloseFont(g_fnt_btn);     g_fnt_btn     = NULL; }
}

/* ── Icon ──────────────────────────────────────────────────────────────────── */

static void icon_load(void)
{
    int w, h, ch;
    unsigned char *px = stbi_load_from_memory(icon_png, (int)icon_png_len, &w, &h, &ch, 4);
    if (!px) { diag("icon: stbi failed"); return; }
    SDL_Surface *surf = SDL_CreateRGBSurfaceWithFormatFrom(px, w, h, 32, w*4,
                                                           SDL_PIXELFORMAT_RGBA32);
    if (!surf) { stbi_image_free(px); return; }
    g_icon = SDL_CreateTextureFromSurface(g_ren, surf);
    SDL_SetTextureBlendMode(g_icon, SDL_BLENDMODE_BLEND);
    SDL_FreeSurface(surf);
    stbi_image_free(px);
    g_icon_w = w; g_icon_h = h;
    diag("icon: %dx%d", w, h);
}

static void icon_free(void)
{
    if (g_icon) { SDL_DestroyTexture(g_icon); g_icon = NULL; }
}

/* ── Background images ─────────────────────────────────────────────────────── */

static SDL_Texture *bg_load_one(const unsigned char *data, unsigned int len)
{
    if (!len) return NULL;
    int w, h, ch;
    unsigned char *px = stbi_load_from_memory(data, (int)len, &w, &h, &ch, 4);
    if (!px) return NULL;
    SDL_Surface *surf = SDL_CreateRGBSurfaceWithFormatFrom(px, w, h, 32, w*4,
                                                           SDL_PIXELFORMAT_RGBA32);
    if (!surf) { stbi_image_free(px); return NULL; }
    SDL_Texture *t = SDL_CreateTextureFromSurface(g_ren, surf);
    if (t) SDL_SetTextureBlendMode(t, SDL_BLENDMODE_NONE);
    SDL_FreeSurface(surf);
    stbi_image_free(px);
    return t;
}

static void bg_load(void)
{
    g_bg_pulling      = bg_load_one(bg_pulling_png,      bg_pulling_png_len);
    g_bg_pushing      = bg_load_one(bg_pushing_png,      bg_pushing_png_len);
    g_bg_done         = bg_load_one(bg_done_png,         bg_done_png_len);
    g_bg_conflict     = bg_load_one(bg_conflict_png,     bg_conflict_png_len);
    g_bg_disconnected = bg_load_one(bg_disconnected_png, bg_disconnected_png_len);
    diag("bg loaded: pulling=%p pushing=%p done=%p conflict=%p disconnected=%p",
         (void*)g_bg_pulling, (void*)g_bg_pushing, (void*)g_bg_done,
         (void*)g_bg_conflict, (void*)g_bg_disconnected);
}

static void bg_free(void)
{
    if (g_bg_pulling)      { SDL_DestroyTexture(g_bg_pulling);      g_bg_pulling      = NULL; }
    if (g_bg_pushing)      { SDL_DestroyTexture(g_bg_pushing);      g_bg_pushing      = NULL; }
    if (g_bg_done)         { SDL_DestroyTexture(g_bg_done);         g_bg_done         = NULL; }
    if (g_bg_conflict)     { SDL_DestroyTexture(g_bg_conflict);     g_bg_conflict     = NULL; }
    if (g_bg_disconnected) { SDL_DestroyTexture(g_bg_disconnected); g_bg_disconnected = NULL; }
}

/* ── Drawing helpers ───────────────────────────────────────────────────────── */

static void set_color(SDL_Color c)
{
    SDL_SetRenderDrawColor(g_ren, c.r, c.g, c.b, c.a);
}

static void fill_rect(int x, int y, int w, int h, SDL_Color c)
{
    set_color(c);
    SDL_Rect r = {x, y, w, h};
    SDL_RenderFillRect(g_ren, &r);
}

static void draw_rect_border(int x, int y, int w, int h, SDL_Color c, int bw)
{
    set_color(c);
    for (int i = 0; i < bw; i++) {
        SDL_Rect r = {x+i, y+i, w-i*2, h-i*2};
        SDL_RenderDrawRect(g_ren, &r);
    }
}

/* Render UTF-8 text into a texture; caller frees the texture. */
static SDL_Texture *make_text(TTF_Font *fnt, const char *text, SDL_Color col)
{
    if (!fnt || !text || !text[0]) return NULL;
    SDL_Surface *s = TTF_RenderUTF8_Blended(fnt, text, col);
    if (!s) return NULL;
    SDL_Texture *t = SDL_CreateTextureFromSurface(g_ren, s);
    SDL_FreeSurface(s);
    return t;
}

/* Render word-wrapped text; max_w is in pixels. */
static SDL_Texture *make_text_wrapped(TTF_Font *fnt, const char *text, SDL_Color col, int max_w)
{
    if (!fnt || !text || !text[0]) return NULL;
    SDL_Surface *s = TTF_RenderUTF8_Blended_Wrapped(fnt, text, col, (Uint32)max_w);
    if (!s) return NULL;
    SDL_Texture *t = SDL_CreateTextureFromSurface(g_ren, s);
    SDL_FreeSurface(s);
    return t;
}

static void blit(SDL_Texture *t, int x, int y)
{
    if (!t) return;
    int w, h; SDL_QueryTexture(t, NULL, NULL, &w, &h);
    SDL_Rect dst = {x, y, w, h};
    SDL_RenderCopy(g_ren, t, NULL, &dst);
}

/* ── Phase state machine ───────────────────────────────────────────────────── */

typedef enum {
    PH_SYNCING_PRE,
    PH_CONFLICT,
    PH_SYNCING_RESOLVE,
    PH_NO_CONNECTION,
    PH_SYNCED_OK,
    PH_PUSHING,
    PH_PUSHED,
} Phase;

static Phase g_phase;

static SDL_Texture *bg_for_phase(void)
{
    switch (g_phase) {
    case PH_SYNCING_PRE:
    case PH_SYNCING_RESOLVE: return g_bg_pulling;
    case PH_PUSHING:         return g_bg_pushing;
    case PH_SYNCED_OK:
    case PH_PUSHED:          return g_bg_done;
    case PH_CONFLICT:        return g_bg_conflict;
    case PH_NO_CONNECTION:   return g_bg_disconnected;
    }
    return NULL;
}

/* Button: label, fill color, result value, screen rect */
typedef struct { char label[80]; SDL_Color color; int result; SDL_Rect r; } Btn;

static Btn  g_btns[3];
static int  g_nbtns  = 0;
static int  g_sel    = 0;
static int  g_done   = -1;   /* -1 = running; 0 = ok; 1 = cancelled */
static int  g_syncing = 0;   /* no interactive buttons shown */
static char g_heading[128];
static char g_body[512];

static int phase_dismissable(void)
{
    return g_phase == PH_SYNCED_OK || g_phase == PH_PUSHED;
}

static Uint32 g_dismiss_at = 0; /* SDL_GetTicks() target for auto-dismiss, 0 = no timer */
static int    g_dirty      = 1; /* render requested */
static int    g_quit       = 0;

/* Panel geometry (computed once in render, used for mouse hit-testing) */
static int g_px, g_py, g_pw, g_ph;

/* ── Dialog content setup ──────────────────────────────────────────────────── */

static void setup_syncing(void)
{
    strncpy(g_heading, "Syncing saves...", sizeof(g_heading)-1);
    strncpy(g_body,
        "Syncing your saves with cloud storage.\n\n"
        "The game will launch automatically when the sync completes.",
        sizeof(g_body)-1);
    g_nbtns = 0; g_syncing = 1; g_dirty = 1;
}

static void setup_synced(void)
{
    strncpy(g_heading, "Saves synced!", sizeof(g_heading)-1);
    strncpy(g_body, "Saves are up to date. Launching game...", sizeof(g_body)-1);
    g_nbtns = 0; g_syncing = 1; g_dirty = 1;
}

static void setup_conflict(const char *game)
{
    snprintf(g_heading, sizeof(g_heading), "%s - Save Conflict", game[0] ? game : "Game");
    snprintf(g_body, sizeof(g_body),
        "Saves have changed on cloud storage since your last sync.\n\n"
        "Both sides will be backed up before any changes are made.");
    g_nbtns = 3; g_syncing = 0;
    strncpy(g_btns[0].label, "(A)  Keep Cloud",    79); g_btns[0].color = C_GREEN; g_btns[0].result = 1;
    strncpy(g_btns[1].label, "(B)  Keep Local",    79); g_btns[1].color = C_RED;   g_btns[1].result = 2;
    strncpy(g_btns[2].label, "(Y)  Cancel Launch", 79); g_btns[2].color = C_GOLD;  g_btns[2].result = 0;
    g_sel = 0; g_dirty = 1;
}

static void setup_no_connection(const char *game, int server_ahead)
{
    snprintf(g_heading, sizeof(g_heading), "%s - No Connection", game[0] ? game : "Game");
    if (server_ahead)
        snprintf(g_body, sizeof(g_body),
            "Could not reach cloud storage before launching.\n\n"
            "WARNING: The server had unsynced changes last time it was reachable.\n"
            "You may be playing with outdated saves.\n\n"
            "Continue launching anyway?");
    else
        snprintf(g_body, sizeof(g_body),
            "Could not reach cloud storage before launching.\n\n"
            "Your local saves are safe but won't be updated from the server.\n"
            "Continue launching anyway?");
    g_nbtns = 2; g_syncing = 0;
    strncpy(g_btns[0].label, "(A)  Continue Anyway", 79); g_btns[0].color = C_BLUE; g_btns[0].result = 1;
    strncpy(g_btns[1].label, "(B)  Cancel Launch",   79); g_btns[1].color = C_RED;  g_btns[1].result = 0;
    g_sel = 0; g_dirty = 1;
}

static void setup_pushing(void)
{
    strncpy(g_heading, "Saving game...", sizeof(g_heading)-1);
    strncpy(g_body, "Pushing your saves to cloud storage.", sizeof(g_body)-1);
    g_nbtns = 0; g_syncing = 1; g_dirty = 1;
}

static void setup_pushed(void)
{
    strncpy(g_heading, "Saves uploaded!", sizeof(g_heading)-1);
    strncpy(g_body, "See you next time.", sizeof(g_body)-1);
    g_nbtns = 0; g_syncing = 1; g_dirty = 1;
}

/* ── Phase advance ─────────────────────────────────────────────────────────── */

static void advance(int result)
{
    g_dismiss_at = 0;
    switch (g_phase) {

    case PH_SYNCING_PRE: {
        clear_poll_file();
        EgsStatus ns = {0}; read_status(&ns);
        if (!strcmp(ns.status, "conflict")) {
            g_phase = PH_CONFLICT;
            setup_conflict(ns.game);
        } else {
            g_phase = PH_SYNCED_OK;
            setup_synced();
            g_dismiss_at = SDL_GetTicks() + 2000;
        }
        return;
    }

    case PH_CONFLICT:
        if (result == 0) { write_cancel(); g_done = 1; g_quit = 1; return; }
        write_choice(result == 1 ? "remote" : "local");
        g_phase = PH_SYNCING_RESOLVE;
        set_poll_file("egs_ready.txt");
        setup_syncing();
        return;

    case PH_SYNCING_RESOLVE:
        clear_poll_file();
        g_phase = PH_SYNCED_OK;
        setup_synced();
        g_dismiss_at = SDL_GetTicks() + 2000;
        return;

    case PH_NO_CONNECTION:
        if (result == 0) { write_cancel(); g_done = 1; }
        else              g_done = 0;
        g_quit = 1;
        return;

    case PH_SYNCED_OK:
        g_done = 0; g_quit = 1;
        return;

    case PH_PUSHING:
        clear_poll_file();
        g_phase = PH_PUSHED;
        setup_pushed();
        g_dismiss_at = SDL_GetTicks() + 3000;
        return;

    case PH_PUSHED:
        g_done = 0; g_quit = 1;
        return;
    }
}

/* ── Render ────────────────────────────────────────────────────────────────── */

static void render(void)
{
    int pad     = S(48);
    int btn_h   = S(72);
    int btn_gap = S(16);
    int acc_h   = S(4);

    /* Background */
    set_color(C_BG);
    SDL_RenderClear(g_ren);

    /* Background image — cover-fit to fill window */
    {
        SDL_Texture *bg = bg_for_phase();
        if (bg) {
            int iw, ih; SDL_QueryTexture(bg, NULL, NULL, &iw, &ih);
            float sx = (float)g_sw / iw, sy = (float)g_sh / ih;
            float sc = sx > sy ? sx : sy;
            int dw = (int)(iw * sc), dh = (int)(ih * sc);
            SDL_Rect dst = { (g_sw - dw)/2, (g_sh - dh)/2, dw, dh };
            SDL_RenderCopy(g_ren, bg, NULL, &dst);
        }
    }

    /* Panel */
    g_pw = g_windowed ? g_sw : g_sw * 7 / 10;
    g_ph = g_windowed ? g_sh : g_sh * 6 / 10;
    g_px = (g_sw - g_pw) / 2;
    g_py = (g_sh - g_ph) / 2;

    fill_rect(g_px, g_py, g_pw, g_ph, C_PANEL);
    fill_rect(g_px, g_py, g_pw, acc_h, C_ACCENT);

    /* Icon (watermark, centered, half panel size, at 20% opacity) */
    if (g_icon) {
        int isz = (g_pw < g_ph ? g_pw : g_ph) / 2;
        SDL_Rect dst = { g_px + (g_pw - isz)/2, g_py + (g_ph - isz)/2, isz, isz };
        SDL_SetTextureAlphaMod(g_icon, 51); /* 20% */
        SDL_RenderCopy(g_ren, g_icon, NULL, &dst);
        SDL_SetTextureAlphaMod(g_icon, 255);
    }

    /* "EXTERNALGAMESYNC" label */
    {
        SDL_Texture *t = make_text(g_fnt_label ? g_fnt_label : g_fnt_body,
                                   "EXTERNALGAMESYNC", C_DIM);
        blit(t, g_px + pad, g_py + S(12));
        SDL_DestroyTexture(t);
    }

    /* Heading */
    {
        SDL_Texture *t = make_text(g_fnt_heading, g_heading, C_WHITE);
        blit(t, g_px + pad, g_py + S(44));
        SDL_DestroyTexture(t);
    }

    /* Divider */
    fill_rect(g_px + pad, g_py + S(106), g_pw - pad*2, S(1), C_DIV);

    /* Body text */
    {
        int body_bottom = g_syncing
            ? g_py + g_ph - pad
            : g_py + g_ph - btn_h - pad*2 - S(8);
        int body_h = body_bottom - (g_py + S(114));
        SDL_Texture *t = make_text_wrapped(g_fnt_body, g_body, C_TEXT, g_pw - pad*2);
        if (t) {
            int tw, th; SDL_QueryTexture(t, NULL, NULL, &tw, &th);
            if (th > body_h) th = body_h; /* clip — SDL_RenderCopy handles this */
            SDL_Rect src = {0, 0, tw, th};
            SDL_Rect dst = {g_px + pad, g_py + S(114), tw, th};
            SDL_RenderCopy(g_ren, t, &src, &dst);
            SDL_DestroyTexture(t);
        }
    }

    /* Buttons */
    if (!g_syncing && g_nbtns > 0) {
        int bw  = (g_pw - pad*2 - btn_gap*(g_nbtns-1)) / g_nbtns;
        int by  = g_py + g_ph - btn_h - pad;
        int bx0 = g_px + pad;

        for (int i = 0; i < g_nbtns; i++) {
            g_btns[i].r.x = bx0 + i*(bw+btn_gap);
            g_btns[i].r.y = by;
            g_btns[i].r.w = bw;
            g_btns[i].r.h = btn_h;

            fill_rect(g_btns[i].r.x, g_btns[i].r.y, bw, btn_h, g_btns[i].color);
            SDL_Color bdr = (i == g_sel) ? C_SEL_BDR : g_btns[i].color;
            int bdr_w = (i == g_sel) ? S(3) : S(1);
            draw_rect_border(g_btns[i].r.x, g_btns[i].r.y, bw, btn_h, bdr, bdr_w);

            SDL_Texture *lt = make_text(g_fnt_btn, g_btns[i].label, C_WHITE);
            if (lt) {
                int lw, lh; SDL_QueryTexture(lt, NULL, NULL, &lw, &lh);
                blit(lt, g_btns[i].r.x + (bw - lw)/2,
                         g_btns[i].r.y + (btn_h - lh)/2);
                SDL_DestroyTexture(lt);
            }
        }

        /* Navigation hint below panel */
        {
            SDL_Texture *ht = make_text(g_fnt_hint ? g_fnt_hint : g_fnt_body,
                "Press button directly  \xc2\xb7  D-Pad/Arrows to navigate  \xc2\xb7  Start/Enter to confirm  \xc2\xb7  Esc to cancel",
                C_DIM);
            if (ht) {
                int hw, hh; SDL_QueryTexture(ht, NULL, NULL, &hw, &hh);
                blit(ht, g_px + (g_pw - hw)/2, g_py + g_ph + S(12));
                SDL_DestroyTexture(ht);
            }
        }
    }

    SDL_RenderPresent(g_ren);
    g_dirty = 0;
}

/* ── Controller ────────────────────────────────────────────────────────────── */

/* Open all connected controllers at startup, re-open on hotplug */
#define MAX_CONTROLLERS 4
static SDL_GameController *g_controllers[MAX_CONTROLLERS];

static void controllers_open_all(void)
{
    for (int i = 0; i < SDL_NumJoysticks(); i++)
        if (SDL_IsGameController(i) && i < MAX_CONTROLLERS)
            if (!g_controllers[i])
                g_controllers[i] = SDL_GameControllerOpen(i);
}

static void controllers_close_all(void)
{
    for (int i = 0; i < MAX_CONTROLLERS; i++)
        if (g_controllers[i]) { SDL_GameControllerClose(g_controllers[i]); g_controllers[i] = NULL; }
}

static void set_sel(int idx)
{
    if (g_nbtns <= 0) return;
    g_sel = (idx + g_nbtns) % g_nbtns;
    g_dirty = 1;
}

static void handle_confirm(void)
{
    if (phase_dismissable()) { advance(0); return; }
    if (!g_syncing && g_nbtns > 0) advance(g_btns[g_sel].result);
}

static void handle_cancel(void)
{
    if (phase_dismissable()) { advance(0); return; }
    if (g_syncing) return;
    int cancel_idx = g_nbtns > 2 ? 2 : g_nbtns - 1;
    advance(g_btns[cancel_idx].result);
}

static void handle_controller_button(SDL_GameControllerButton btn)
{
    if (g_syncing) {
        if (phase_dismissable() &&
            (btn == SDL_CONTROLLER_BUTTON_A || btn == SDL_CONTROLLER_BUTTON_B ||
             btn == SDL_CONTROLLER_BUTTON_Y || btn == SDL_CONTROLLER_BUTTON_START))
            advance(0);
        return;
    }
    switch (btn) {
    case SDL_CONTROLLER_BUTTON_DPAD_LEFT:  set_sel(g_sel - 1); break;
    case SDL_CONTROLLER_BUTTON_DPAD_RIGHT: set_sel(g_sel + 1); break;
    case SDL_CONTROLLER_BUTTON_START:      handle_confirm(); break;
    case SDL_CONTROLLER_BUTTON_A:          if (g_nbtns > 0) advance(g_btns[0].result); break;
    case SDL_CONTROLLER_BUTTON_B:          if (g_nbtns > 1) advance(g_btns[1].result); break;
    case SDL_CONTROLLER_BUTTON_Y:          if (g_nbtns > 2) advance(g_btns[2].result); break;
    default: break;
    }
}

/* ── Event handling ────────────────────────────────────────────────────────── */

static void handle_event(const SDL_Event *ev)
{
    switch (ev->type) {
    case SDL_QUIT:
        write_cancel(); g_done = 1; g_quit = 1;
        break;

    case SDL_KEYDOWN:
        switch (ev->key.keysym.sym) {
        case SDLK_LEFT: case SDLK_UP:    if (!g_syncing) set_sel(g_sel - 1); break;
        case SDLK_RIGHT: case SDLK_DOWN: if (!g_syncing) set_sel(g_sel + 1); break;
        case SDLK_RETURN: case SDLK_SPACE: handle_confirm(); break;
        case SDLK_ESCAPE: handle_cancel(); break;
        }
        break;

    case SDL_MOUSEMOTION:
        if (!g_syncing) {
            int x = ev->motion.x, y = ev->motion.y;
            for (int i = 0; i < g_nbtns; i++)
                if (x >= g_btns[i].r.x && x < g_btns[i].r.x + g_btns[i].r.w &&
                    y >= g_btns[i].r.y && y < g_btns[i].r.y + g_btns[i].r.h)
                    set_sel(i);
        }
        break;

    case SDL_MOUSEBUTTONDOWN:
        if (ev->button.button == SDL_BUTTON_LEFT) {
            if (g_syncing) { if (phase_dismissable()) advance(0); break; }
            int x = ev->button.x, y = ev->button.y;
            for (int i = 0; i < g_nbtns; i++)
                if (x >= g_btns[i].r.x && x < g_btns[i].r.x + g_btns[i].r.w &&
                    y >= g_btns[i].r.y && y < g_btns[i].r.y + g_btns[i].r.h)
                    advance(g_btns[i].result);
        }
        break;

    case SDL_CONTROLLERBUTTONDOWN:
        handle_controller_button((SDL_GameControllerButton)ev->cbutton.button);
        break;

    case SDL_CONTROLLERDEVICEADDED:
        controllers_open_all();
        break;

    case SDL_WINDOWEVENT:
        if (ev->window.event == SDL_WINDOWEVENT_EXPOSED) g_dirty = 1;
        break;
    }
}

/* ── Main event loop ───────────────────────────────────────────────────────── */

static void run_loop(void)
{
    g_quit = 0; g_dirty = 1;
    while (!g_quit) {
        SDL_Event ev;
        /* 100ms timeout — IPC polling rate */
        if (SDL_WaitEventTimeout(&ev, 100))
            handle_event(&ev);
        /* drain any queued events */
        while (SDL_PollEvent(&ev))
            handle_event(&ev);

        /* IPC poll */
        if (g_poll_file[0] && poll_file_exists())
            advance(0);

        /* Auto-dismiss */
        if (g_dismiss_at && SDL_GetTicks() >= g_dismiss_at)
            advance(0);

        if (g_dirty && !g_quit)
            render();
    }
}

/* ── Window management ─────────────────────────────────────────────────────── */

static int window_open(void)
{
    if (g_win) return 1;

    Uint32 flags = SDL_WINDOW_BORDERLESS | SDL_WINDOW_INPUT_FOCUS;
    if (!g_windowed) flags |= SDL_WINDOW_FULLSCREEN_DESKTOP;

    int wx = SDL_WINDOWPOS_CENTERED, wy = SDL_WINDOWPOS_CENTERED;
    g_win = SDL_CreateWindow("ExternalGameSync", wx, wy, g_sw, g_sh, flags);
    if (!g_win) { diag("SDL_CreateWindow failed: %s", SDL_GetError()); return 0; }

    g_ren = SDL_CreateRenderer(g_win, -1,
                               SDL_RENDERER_ACCELERATED | SDL_RENDERER_PRESENTVSYNC);
    if (!g_ren) {
        g_ren = SDL_CreateRenderer(g_win, -1, SDL_RENDERER_SOFTWARE);
        if (!g_ren) { diag("SDL_CreateRenderer failed: %s", SDL_GetError()); return 0; }
    }

    SDL_SetRenderDrawBlendMode(g_ren, SDL_BLENDMODE_BLEND);
    SDL_RaiseWindow(g_win);
    diag("window opened: %dx%d", g_sw, g_sh);
    return 1;
}

static void window_close(void)
{
    bg_free();
    icon_free();
    if (g_ren) { SDL_DestroyRenderer(g_ren); g_ren = NULL; }
    if (g_win) { SDL_DestroyWindow(g_win);   g_win = NULL; }
}

/* ── Pre-game sequence ─────────────────────────────────────────────────────── */
/* Returns 0 = ok to launch, 1 = cancelled */

static int run_pre_game(const EgsStatus *s0)
{
    EgsStatus s; memcpy(&s, s0, sizeof s);
    int was_syncing = !strcmp(s.status, "syncing");

    /* Fast path: ready file already written before we started */
    char ready[4096]; ipc_path("egs_ready.txt", ready, sizeof(ready));
    if (file_exists(ready)) {
        file_delete(ready);
        memset(&s, 0, sizeof s); read_status(&s);
        was_syncing = 1;
    }

    if (!strcmp(s.status, "syncing")) {
        g_phase = PH_SYNCING_PRE;
        set_poll_file("egs_ready.txt");
        setup_syncing();
    } else if (!strcmp(s.status, "conflict")) {
        g_phase = PH_CONFLICT;
        setup_conflict(s.game);
    } else if (!strcmp(s.status, "no_connection")) {
        int sa = (!strcmp(s.last_status, "cloud_ahead") || !strcmp(s.last_status, "conflict"));
        g_phase = PH_NO_CONNECTION;
        setup_no_connection(s.game, sa);
    } else if (was_syncing) {
        g_phase = PH_SYNCED_OK;
        setup_synced();
        g_dismiss_at = SDL_GetTicks() + 2000;
    } else {
        return 0; /* nothing to show */
    }

    if (!window_open()) return 0;
    if (!fonts_load()) diag("warning: fonts not loaded — text will not render");
    icon_load();
    bg_load();
    run_loop();
    return (g_done == 1) ? 1 : 0;
}

/* ── Post-game sequence ────────────────────────────────────────────────────── */

static void run_post_game(void)
{
    write_push_signal();
    g_phase = PH_PUSHING;
    g_done = -1;
    set_poll_file("egs_push_done.txt");
    setup_pushing();
    if (!window_open()) return;
    if (!fonts_load()) diag("warning: fonts not loaded");
    icon_load();
    bg_load();
    run_loop();
}

/* ── Windows-only: launch and wait for the game process ───────────────────── */

#ifdef _WIN32

static BOOL any_process_in_dir(const char *dir)
{
    size_t dirlen = strlen(dir);
    HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
    if (snap == INVALID_HANDLE_VALUE) return FALSE;
    PROCESSENTRY32 pe = {sizeof(pe)};
    BOOL found = FALSE;
    if (Process32First(snap, &pe)) {
        do {
            if (pe.th32ProcessID <= 4) continue;
            HANDLE ph = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, pe.th32ProcessID);
            if (!ph) continue;
            char path[8192] = {0}; DWORD sz = sizeof(path);
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
    char cmd[4100]; snprintf(cmd, sizeof(cmd), "\"%s\"", exe);
    diag("run_wait: %s", cmd);

    char work_dir[4096]; strncpy(work_dir, exe, sizeof(work_dir)-1);
    work_dir[sizeof(work_dir)-1] = 0;
    char *sep = strrchr(work_dir, '\\');
    if (!sep) sep = strrchr(work_dir, '/');
    if (sep) *sep = 0; else work_dir[0] = 0;

    HANDLE job  = CreateJobObjectA(NULL, NULL);
    HANDLE iocp = job ? CreateIoCompletionPort(INVALID_HANDLE_VALUE, NULL, 0, 1) : NULL;
    if (job && iocp) {
        JOBOBJECT_ASSOCIATE_COMPLETION_PORT jacp = {0};
        jacp.CompletionKey  = job;
        jacp.CompletionPort = iocp;
        SetInformationJobObject(job, JobObjectAssociateCompletionPortInformation,
                                &jacp, sizeof(jacp));
    }

    STARTUPINFOA si = {sizeof(si)}; PROCESS_INFORMATION pi = {0};
    DWORD flags = NORMAL_PRIORITY_CLASS | (job ? CREATE_SUSPENDED : 0);
    if (!CreateProcessA(NULL, cmd, NULL, NULL, FALSE, flags,
                        NULL, work_dir[0] ? work_dir : NULL, &si, &pi)) {
        diag("run_wait: CreateProcess failed: %lu", GetLastError());
        if (iocp) CloseHandle(iocp); if (job) CloseHandle(job);
        return -1;
    }
    if (job) { AssignProcessToJobObject(job, pi.hProcess); ResumeThread(pi.hThread); }

    WaitForSingleObject(pi.hProcess, INFINITE);
    DWORD code = 0; GetExitCodeProcess(pi.hProcess, &code);
    CloseHandle(pi.hProcess); CloseHandle(pi.hThread);

    if (job && iocp) {
        DWORD msg; ULONG_PTR key; LPOVERLAPPED ov;
        int ticks = 172800;
        while (ticks-- > 0) {
            if (!GetQueuedCompletionStatus(iocp, &msg, &key, &ov, 500)) continue;
            if ((HANDLE)key == job && msg == JOB_OBJECT_MSG_ACTIVE_PROCESS_ZERO) break;
        }
        CloseHandle(iocp); CloseHandle(job);
    }

    if (work_dir[0]) {
        int clear = 0, ticks = 28800;
        while (clear < 2 && ticks-- > 0) { Sleep(500); clear = any_process_in_dir(work_dir) ? 0 : clear+1; }
    }
    return (int)code;
}

#endif /* _WIN32 */

/* ── Entry point ───────────────────────────────────────────────────────────── */

int main(int argc, char *argv[])
{
    SDL_SetMainReady();
    SDL_Init(SDL_INIT_VIDEO | SDL_INIT_GAMECONTROLLER | SDL_INIT_EVENTS);
    TTF_Init();

    ipc_init();   /* must run before diag_open so /tmp/externalgamesync/ exists */
    diag_open();

    EgsStatus s = {0};
    read_status(&s);
    init_scale(s.fullscreen);

    controllers_open_all();

#ifdef _WIN32
    /* Windows: always run the full flow (pre → game → post) */
    (void)argc; (void)argv;
    diag("mode: windows full flow  game_exe=%s", s.game_exe);

    int cancelled = run_pre_game(&s);
    window_close();
    fonts_free();

    if (cancelled) { diag("cancelled"); controllers_close_all(); TTF_Quit(); SDL_Quit(); return 1; }

    int exit_code = 0;
    if (s.game_exe[0]) {
        diag("launching: %s", s.game_exe);
        exit_code = run_wait(s.game_exe);
        diag("run_wait: %d", exit_code);
    } else {
        diag("no game_exe — skipping launch");
    }

    run_post_game();
    window_close();
    fonts_free();

    controllers_close_all();
    TTF_Quit();
    SDL_Quit();
    return exit_code;

#else
    /* Linux: two-phase invocation: pre | post */
    const char *mode = (argc >= 2) ? argv[1] : "pre";
    diag("mode: %s", mode);

    int rc = 0;
    if (!strcmp(mode, "post")) {
        run_post_game();
    } else {
        rc = run_pre_game(&s);
    }

    window_close();
    fonts_free();
    controllers_close_all();
    TTF_Quit();
    SDL_Quit();
    return rc; /* 0 = ok, 1 = cancelled */
#endif
}
