--[[
  Play Along - VLC extension (Lua) that keeps playback in sync across several devices.

  Install (VLC 3.x, Qt interface): copy play_along.lua AND the lang/ folder to
    Windows : %APPDATA%\vlc\lua\extensions\
    Linux   : ~/.local/share/vlc/lua/extensions/
    macOS   : ~/Library/Application Support/org.videolan.vlc/lua/extensions/
  Then Tools > Plugins and extensions > Active Extensions > Reload extensions, and enable
  it under View > Play Along.

  Language: English is built in (the strings in this file are the keys). Translations live
  in lang/<code>.json next to this script, as {"English text": "translated text"}. The
  language is detected from VLC's own UI strings (see detect_language); it can be forced
  with the environment variable PLAY_ALONG_LANG (e.g. "en" or "pt").

  Technical notes:
  * VLC Lua extensions have no timer and no sleep. Every callback runs, in order, on a
    single extension thread with a 10 s watchdog.
  * The "clock" is the server: each /api/sync is a long poll of ~TICK_MS. To chain the
    next cycle without a timer, the extension changes a harmless meta of the playing item
    (item:set_meta); VLC (playlist -> Qt) calls meta_changed() back and that runs the
    next cycle.
  * The Qt interface delivers two meta_changed per change, so the cycle is only
    rescheduled every second event (S.since). Extra events become a quick tick (wait 0)
    and never multiply the chain.
--]]

local VERSION        = "0.2.0"
local DEFAULT_SERVER = "127.0.0.1"   -- host or host:port; without port, DEFAULT_PORT is used
local DEFAULT_PORT   = 8765
local TICK_MS        = 1000   -- long-poll length (cycle pace)
local MAX_FAILURES   = 3      -- consecutive network failures before stopping and asking to reconnect
local GRACE_S        = 3      -- after applying a server command, ignore local-action detection this long
local JUMP_THRESHOLD = 2.5    -- s: position jump treated as a user seek
local SEEK_THRESHOLD = 0.5    -- s: minimum difference to perform a correction seek
local META_KEY       = "setting"   -- harmless meta used to trigger the next cycle
local PLAYING_S, PAUSE_S = 2, 3    -- VLC input_state_e

-- IMPORTANT: when VLC scans the extensions folder to build the View menu it runs this file
-- in an EMPTY Lua state (no string/table/os/debug libraries, not even type()). Anything
-- executed at the top level of this file must therefore only define functions and tables;
-- every call to a library goes inside a function.

-- Directory of this script, used to find lang/*.json (the chunk name is "@<path>").
local function script_dir()
  local ok, dir = pcall(function()
    local info = debug.getinfo(1, "S")
    local src = info and info.source
    if type(src) == "string" and src:sub(1, 1) == "@" then
      return src:sub(2):match("^(.*)[/\\][^/\\]+$")
    end
    return nil
  end)
  if ok then return dir end
  return nil
end

-- ===================================================================== descriptor
function descriptor()
  return {
    title        = "Play Along",
    version      = VERSION,
    author       = "Play Along",
    shortdesc    = "Play Along - synchronized playback",
    description  = "Keeps play/pause, position and speed in sync across several VLCs "
                   .. "through a Play Along server (anarchy or monarchy).",
    capabilities = { "input-listener", "playing-listener" },
  }
end

-- ===================================================================== JSON (minimal)
local json = {}
do
  local escapes = { ['"'] = '\\"', ['\\'] = '\\\\', ['\b'] = '\\b', ['\f'] = '\\f',
                    ['\n'] = '\\n', ['\r'] = '\\r', ['\t'] = '\\t' }
  local function enc_str(s)
    return '"' .. s:gsub('[%c"\\]', function(c)
      return escapes[c] or string.format("\\u%04x", c:byte())
    end) .. '"'
  end
  local function is_array(t)
    local n = 0
    for k in pairs(t) do
      if type(k) ~= "number" or k < 1 or math.floor(k) ~= k then return false end
      n = n + 1
    end
    return n == #t
  end
  function json.encode(v)
    local tv = type(v)
    if v == nil then return "null"
    elseif tv == "boolean" then return v and "true" or "false"
    elseif tv == "number" then
      if v ~= v or v == math.huge or v == -math.huge then return "null" end
      if math.floor(v) == v and math.abs(v) < 1e15 then return string.format("%.0f", v) end
      return string.format("%.14g", v)
    elseif tv == "string" then return enc_str(v)
    elseif tv == "table" then
      local out = {}
      if is_array(v) then
        for i = 1, #v do out[i] = json.encode(v[i]) end
        return "[" .. table.concat(out, ",") .. "]"
      end
      for k, val in pairs(v) do out[#out + 1] = enc_str(tostring(k)) .. ":" .. json.encode(val) end
      return "{" .. table.concat(out, ",") .. "}"
    end
    return "null"
  end

  local function utf8_char(cp)
    if cp < 0x80 then return string.char(cp)
    elseif cp < 0x800 then return string.char(0xC0 + math.floor(cp / 0x40), 0x80 + cp % 0x40)
    else return string.char(0xE0 + math.floor(cp / 0x1000), 0x80 + math.floor(cp / 0x40) % 0x40,
                            0x80 + cp % 0x40) end
  end
  local unescapes = { b = "\b", f = "\f", n = "\n", r = "\r", t = "\t",
                      ['"'] = '"', ["\\"] = "\\", ["/"] = "/" }
  local function skip_ws(s, i) return s:find("[^ \t\r\n]", i) or (#s + 1) end
  local function dec_str(s, i)
    local out, j = {}, i + 1
    while true do
      local k = s:find('["\\]', j)
      if not k then error("JSON: unterminated string") end
      out[#out + 1] = s:sub(j, k - 1)
      if s:sub(k, k) == '"' then return table.concat(out), k + 1 end
      local e = s:sub(k + 1, k + 1)
      if e == "u" then
        out[#out + 1] = utf8_char(tonumber(s:sub(k + 2, k + 5), 16) or 63)
        j = k + 6
      elseif unescapes[e] then
        out[#out + 1] = unescapes[e]
        j = k + 2
      else
        error("JSON: invalid escape")
      end
    end
  end
  local decode_value
  decode_value = function(s, i)
    i = skip_ws(s, i)
    local c = s:sub(i, i)
    if c == "{" then
      local obj = {}
      i = skip_ws(s, i + 1)
      if s:sub(i, i) == "}" then return obj, i + 1 end
      while true do
        i = skip_ws(s, i)
        if s:sub(i, i) ~= '"' then error("JSON: key expected at position " .. i) end
        local k
        k, i = dec_str(s, i)
        i = skip_ws(s, i)
        if s:sub(i, i) ~= ":" then error("JSON: ':' expected at position " .. i) end
        local v
        v, i = decode_value(s, i + 1)
        obj[k] = v
        i = skip_ws(s, i)
        c = s:sub(i, i)
        if c == "," then i = i + 1
        elseif c == "}" then return obj, i + 1
        else error("JSON: ',' or '}' expected at position " .. i) end
      end
    elseif c == "[" then
      local arr = {}
      i = skip_ws(s, i + 1)
      if s:sub(i, i) == "]" then return arr, i + 1 end
      while true do
        local v
        v, i = decode_value(s, i)
        arr[#arr + 1] = v
        i = skip_ws(s, i)
        c = s:sub(i, i)
        if c == "," then i = i + 1
        elseif c == "]" then return arr, i + 1
        else error("JSON: ',' or ']' expected at position " .. i) end
      end
    elseif c == '"' then return dec_str(s, i)
    elseif s:sub(i, i + 3) == "true" then return true, i + 4
    elseif s:sub(i, i + 4) == "false" then return false, i + 5
    elseif s:sub(i, i + 3) == "null" then return nil, i + 4
    else
      local a, b = s:find("^-?%d+%.?%d*[eE]?[-+]?%d*", i)
      if not a then error("JSON: unexpected value at position " .. i) end
      return tonumber(s:sub(a, b)), b + 1
    end
  end
  function json.decode(s)
    local v = decode_value(s, 1)
    return v
  end
end

-- ===================================================================== utilities
local function log(msg)  vlc.msg.info("[play_along] " .. tostring(msg)) end
local function dbg(msg)  vlc.msg.dbg("[play_along] " .. tostring(msg)) end
local function trim(s)   return (tostring(s or ""):gsub("^%s+", ""):gsub("%s+$", "")) end

local function fmt_time(sec)
  if type(sec) ~= "number" or sec < 0 then return "--:--" end
  sec = math.floor(sec + 0.5)
  local h = math.floor(sec / 3600)
  local m = math.floor((sec % 3600) / 60)
  local s = sec % 60
  if h > 0 then return string.format("%d:%02d:%02d", h, m, s) end
  return string.format("%02d:%02d", m, s)
end

local function html_escape(s)
  return (tostring(s):gsub("[&<>]", { ["&"] = "&amp;", ["<"] = "&lt;", [">"] = "&gt;" }))
end

local function default_name()
  return os.getenv("COMPUTERNAME") or os.getenv("HOSTNAME") or os.getenv("USERNAME")
         or os.getenv("USER") or "VLC"
end

-- Reads a whole file as a string (UTF-8 path safe through vlc.io when available).
local function read_file(path)
  local ok, data = pcall(function()
    local f = vlc.io.open(path, "r")
    if not f then return nil end
    local s = f:read("*a")
    f:close()
    return s
  end)
  if ok and type(data) == "string" then return data end
  local f = io.open(path, "rb")
  if not f then return nil end
  local s = f:read("*a")
  f:close()
  return s
end

local function write_file(path, data)
  local ok = pcall(function()
    local f = vlc.io.open(path, "w")
    if not f then error("no vlc.io") end
    f:write(data)
    f:close()
  end)
  if ok then return true end
  local f = io.open(path, "wb")
  if not f then return false end
  f:write(data)
  f:close()
  return true
end

-- ===================================================================== state
local S = {
  phase         = "idle",      -- "idle" | "session"
  code          = nil,
  policy        = nil,         -- "anarquia" | "monarquia" (server names)
  host          = "127.0.0.1",
  port          = DEFAULT_PORT,
  server_text   = DEFAULT_SERVER,
  device_id     = nil,
  device_name   = nil,
  lang          = nil,         -- "en" | "pt"
  lang_source   = nil,         -- "env" | "detected" | "saved" | "default"
  saved_lang    = nil,
  since         = 0,           -- meta_changed events since the last reschedule
  tick_flip     = false,
  last          = nil,         -- previous player reading {playing, position, rate, t}
  server_now    = nil,         -- server clock from the last response
  grace_until   = 0,           -- os.time() until which local actions are not detected
  failures      = 0,
  session_info  = nil,
  last_msg      = "",
  last_tick_wall = 0,
}
local dlg, W = nil, {}

-- Shown whenever the window is used without a media loaded.
local NO_MEDIA_MSG = "No media is playing. Start playback and pause it at the beginning before starting or joining a session."

-- ===================================================================== i18n
local LANG = {}   -- English key -> translated text (empty table = English)

-- T("English text", ...) returns the translation (or the key itself) formatted with the
-- extra arguments, if any.
local function T(key, ...)
  local s = LANG[key] or key
  if select("#", ...) > 0 then return string.format(s, ...) end
  return s
end

local function normalize_lang(l)
  if type(l) ~= "string" then return nil end
  l = l:lower()
  if l:match("^pt") then return "pt" end
  if l:match("^en") then return "en" end
  return nil
end

-- The VLC Lua API has no "current language" call, so the language is inferred from strings
-- VLC itself translates with gettext. Values taken from the VLC 3.0.x .po files.
-- 1) Names of the playlist root nodes ("Playlist" / "Media Library"): always available,
--    even with no media open.
local NODE_TO_LANG = {
  ["Playlist"] = "en", ["Media Library"] = "en",
  ["Lista de reprodução"] = "pt", ["Biblioteca de Mídia"] = "pt_BR", ["Mediateca"] = "pt_PT",
}
-- 2) The "Disable" entry of the audio-track list: needs a media with audio.
local DISABLE_TO_LANG = {
  ["Disable"] = "en", ["Desabilitar"] = "pt_BR", ["Desativar"] = "pt_PT",
}

local function detect_language()
  local okr, root = pcall(vlc.playlist.get, "root")
  if okr and type(root) == "table" and type(root.children) == "table" then
    for _, node in ipairs(root.children) do
      local lang = NODE_TO_LANG[tostring(node.name)]
      if lang then return lang end
    end
  end
  local ok, input = pcall(vlc.object.input)
  if not ok or not input then return nil end
  local okl, _, texts = pcall(vlc.var.get_list, input, "audio-es")
  if not okl or type(texts) ~= "table" then return nil end
  for _, t in ipairs(texts) do
    if DISABLE_TO_LANG[t] then return DISABLE_TO_LANG[t] end
  end
  return nil
end

local function lang_dirs()
  local dirs = {}
  local here = script_dir()
  if here then dirs[#dirs + 1] = here .. "/lang" end
  pcall(function() dirs[#dirs + 1] = vlc.config.userdatadir() .. "/lua/extensions/lang" end)
  pcall(function() dirs[#dirs + 1] = vlc.config.datadir() .. "/lua/extensions/lang" end)
  return dirs
end

local function load_language(lang)
  if lang == "en" then return {} end
  for _, dir in ipairs(lang_dirs()) do
    local data = read_file(dir .. "/" .. lang .. ".json")
    if data then
      data = data:gsub("^\239\187\191", "")       -- strip a UTF-8 BOM, if any
      local ok, tbl = pcall(json.decode, data)
      if ok and type(tbl) == "table" then
        log("language '" .. lang .. "' loaded from " .. dir)
        return tbl
      end
      log("could not parse " .. dir .. "/" .. lang .. ".json: " .. tostring(tbl))
    end
  end
  log("no translation file for '" .. lang .. "', using English")
  return nil
end

local function resolve_language()
  local env = normalize_lang(os.getenv("PLAY_ALONG_LANG"))
  if env then return env, "env" end
  local det = normalize_lang(detect_language())
  if det then return det, "detected" end
  if S.saved_lang then return S.saved_lang, "saved" end
  return "en", "default"
end

-- ===================================================================== persisted config
local function config_path()
  return vlc.config.userdatadir() .. "/play_along.json"
end

local function load_config()
  local ok, data = pcall(function()
    local s = read_file(config_path())
    return s and json.decode(s) or nil
  end)
  if ok and type(data) == "table" then
    if type(data.server) == "string" and data.server ~= "" then S.server_text = data.server end
    if type(data.name) == "string" and data.name ~= "" then S.device_name = data.name end
    S.saved_lang = normalize_lang(data.lang)
  end
end

local function save_config()
  pcall(function()
    write_file(config_path(), json.encode({ server = S.server_text, name = S.device_name, lang = S.lang }))
  end)
end

-- ===================================================================== HTTP (raw TCP)
local function http_post_json(host, port, path, payload, fire_and_forget)
  local body = json.encode(payload)
  local fd = vlc.net.connect_tcp(host, port)
  if not fd or fd < 0 then
    return nil, T("could not connect to %s:%s", host, tostring(port))
  end
  local req = table.concat({
    "POST " .. path .. " HTTP/1.0",
    "Host: " .. host .. ":" .. tostring(port),
    "Content-Type: application/json; charset=utf-8",
    "Content-Length: " .. #body,
    "Connection: close",
    "",
    body,
  }, "\r\n")

  local sent = 0
  while sent < #req do
    local n = vlc.net.send(fd, req:sub(sent + 1))
    if not n or n <= 0 then
      vlc.net.close(fd)
      return nil, T("failed to send the request")
    end
    sent = sent + n
  end
  if fire_and_forget then
    vlc.net.close(fd)
    return {}, nil
  end

  local chunks = {}
  while true do
    local fds = { [fd] = vlc.net.POLLIN }
    local r = vlc.net.poll(fds)          -- blocks until data arrives (or the extension is interrupted)
    if not r or r < 0 then
      vlc.net.close(fd)
      return nil, T("wait interrupted")
    end
    local chunk = vlc.net.recv(fd, 65536)
    if not chunk then break end          -- end of response (server closes the connection)
    chunks[#chunks + 1] = chunk
  end
  vlc.net.close(fd)

  local resp = table.concat(chunks)
  local head, rest = resp:match("^(.-)\r?\n\r?\n(.*)$")
  if not head then return nil, T("invalid HTTP response") end
  local status = tonumber(head:match("^HTTP/%d%.%d%s+(%d+)"))
  local ok, data = pcall(json.decode, rest)
  if not ok or type(data) ~= "table" then
    return nil, T("unexpected server response (HTTP %s)", tostring(status))
  end
  return data, nil, status
end

local function api(path, payload, fire_and_forget)
  return http_post_json(S.host, S.port, path, payload, fire_and_forget)
end

-- ===================================================================== player
local function get_input()
  local ok, input = pcall(vlc.object.input)
  if ok then return input end
  return nil
end

local function read_player()
  local input = get_input()
  if not input then return nil end
  local ok, cur = pcall(function()
    local item   = vlc.input.item()
    local t      = tonumber(vlc.var.get(input, "time")) or 0
    local len    = tonumber(vlc.var.get(input, "length")) or 0
    local rate   = tonumber(vlc.var.get(input, "rate")) or 1
    local status = vlc.playlist.status()
    return {
      playing  = (status == "playing"),
      position = t / 1e6,
      rate     = rate,
      length   = len / 1e6,
      name     = (item and item:name()) or "",
    }
  end)
  if ok then return cur end
  return nil
end

local function seek_to(sec)
  local input = get_input()
  if not input then return end
  if sec < 0 then sec = 0 end
  vlc.var.set(input, "time", math.floor(sec * 1e6 + 0.5))
end

local function set_playing(flag)
  local input = get_input()
  if not input then return end
  vlc.var.set(input, "state", flag and PLAYING_S or PAUSE_S)
end

local function set_rate(r)
  local input = get_input()
  if not input then return end
  vlc.var.set(input, "rate", r)
end

local function state_of(cur)
  return { playing = cur.playing, position = cur.position, rate = cur.rate }
end

local function media_of(cur)
  return { name = cur.name, length = cur.length }
end

-- ===================================================================== UI
local render_status, build_dialog

local function set_msg(msg)
  S.no_media_warning = false
  S.last_msg = tostring(msg)
  log(msg)
  render_status()
end

-- The "no media" warning is kept as a flag so it can be re-translated when the language
-- changes and cleared as soon as a media shows up (see input_changed).
local function warn_no_media()
  set_msg(T(NO_MEDIA_MSG))
  S.no_media_warning = true
end

-- Protocol policy values are "anarchy"/"monarchy"; the Portuguese names are accepted too.
local POLICY_NAMES = { anarchy = "anarchy", monarchy = "monarchy",
                       anarquia = "anarchy", monarquia = "monarchy" }
local function policy_label(p)
  return T(POLICY_NAMES[p] or tostring(p))
end
local function is_monarchy()
  return POLICY_NAMES[S.policy] == "monarchy"
end

render_status = function()
  if not W.status then return end
  local parts = {}
  if S.phase ~= "session" then
    parts[#parts + 1] = "<b>" .. html_escape(T("Not in a session.")) .. "</b> " .. html_escape(
      T("Open the media, start playback and pause it at the beginning, then start a session or join one with its code."))
  else
    local info = S.session_info
    local role
    if info and info.you_are_owner then
      role = "<b>" .. html_escape(T("you are the monarch (you control the session)")) .. "</b>"
    elseif is_monarchy() then
      role = html_escape(T("follower (just follows)"))
    else
      role = html_escape(T("participant (everyone controls)"))
    end
    parts[#parts + 1] = "<b>" .. html_escape(T("Session %s", S.code or "?")) .. "</b> &middot; "
                        .. html_escape(policy_label(S.policy)) .. " &middot; " .. role
    local cur = read_player()
    if cur then
      parts[#parts + 1] = string.format("<b>%s</b> %s / %s &middot; %.2fx &middot; %s",
                                        html_escape(T("You:")), fmt_time(cur.position), fmt_time(cur.length),
                                        cur.rate, html_escape(cur.playing and T("playing") or T("paused")))
    end
    if info and type(info.state) == "table" then
      parts[#parts + 1] = string.format("<b>%s</b> %s &middot; %.2fx &middot; %s",
                                        html_escape(T("Session:")), fmt_time(info.state.position),
                                        tonumber(info.state.rate) or 1,
                                        html_escape(info.state.playing and T("session playing") or T("session paused")))
    end
    if info and type(info.devices) == "table" then
      parts[#parts + 1] = "<b>" .. html_escape(T("Devices (%d):", #info.devices)) .. "</b>"
      for _, d in ipairs(info.devices) do
        local drift = (type(d.drift) == "number") and (" &middot; " .. html_escape(T("drift %+.1f s", d.drift))) or ""
        parts[#parts + 1] = string.format("&nbsp;&nbsp;&bull; %s%s &mdash; %s, %s%s",
                                          html_escape(d.name or "?"),
                                          d.is_owner and (" " .. html_escape(T("(monarch)"))) or "",
                                          fmt_time(d.position),
                                          html_escape(d.playing and T("playing") or T("paused")), drift)
      end
    end
  end
  if S.last_msg ~= "" then
    parts[#parts + 1] = "<i>" .. html_escape(S.last_msg) .. "</i>"
  end
  pcall(function()
    W.status:set_text(table.concat(parts, "<br/>"))
    if dlg then dlg:update() end
  end)
end

local function read_inputs()
  local server = trim(W.server and W.server:get_text() or S.server_text)
  server = server:gsub("^https?://", ""):gsub("/+$", "")
  if server == "" then server = DEFAULT_SERVER end
  -- Accepts "host", "host:port", "[ipv6]" or "[ipv6]:port". Without a port, DEFAULT_PORT.
  local host, port = server:match("^%[([^%]]+)%]:(%d+)$")          -- [ipv6]:port
  if not host then host = server:match("^%[([^%]]+)%]$") end          -- [ipv6]
  if not host and select(2, server:gsub(":", "")) == 1 then
    host, port = server:match("^([^:]+):(%d+)$")                     -- host:port
  end
  if not host then host = server end                                 -- host (or bare ipv6)
  S.server_text = server
  S.host = host
  S.port = tonumber(port) or DEFAULT_PORT

  local name = trim(W.name and W.name:get_text() or S.device_name)
  if name == "" then name = default_name() end
  S.device_name = name
  save_config()
end

-- Applies a language; returns true when it changed.
local function apply_language(lang)
  if lang == S.lang then return false end
  LANG = load_language(lang) or {}
  S.lang = (next(LANG) == nil and lang ~= "en") and "en" or lang
  save_config()
  return true
end

-- Rebuilds the dialog keeping what the user typed (used when the language changes).
local function rebuild_dialog()
  local server = (W.server and W.server:get_text()) or S.server_text
  local name = (W.name and W.name:get_text()) or S.device_name
  local code = (W.code and W.code:get_text()) or (S.code or "")
  if dlg then pcall(function() dlg:delete() end) end
  dlg, W = nil, {}
  build_dialog(server, name, code)
  if S.no_media_warning then S.last_msg = T(NO_MEDIA_MSG) end
  render_status()
end

-- Picks the language (env > detected from VLC > saved > English). Detection needs a media,
-- so this is called again whenever one shows up, until it succeeds.
local function refresh_language()
  if S.lang_source == "env" or S.lang_source == "detected" then return end
  local lang, source = resolve_language()
  S.lang_source = source
  if apply_language(lang) and dlg then rebuild_dialog() end
end

-- ===================================================================== sync
local function describe_action(a)
  if a.type == "pause" then return T("local pause")
  elseif a.type == "play" then return T("local resume")
  elseif a.type == "seek" then return T("local seek to %s", fmt_time(a.position))
  elseif a.type == "rate" then return T("local speed %.2fx", a.rate or 1) end
  return tostring(a.type)
end

-- Server error codes are stable identifiers; translate the known ones for the window.
local ERROR_KEYS = {
  session_not_found  = "session not found",
  device_id_required = "device id required",
  invalid_policy     = "invalid policy",
  owner_only         = "only the session owner can do that",
  unknown_route      = "unknown route",
  invalid_json       = "invalid JSON",
  internal_error     = "internal server error",
}
local function describe_error(err)
  return T(ERROR_KEYS[tostring(err)] or tostring(err))
end

local function detect_action(last, cur, elapsed)
  if cur.playing ~= last.playing then
    return { type = cur.playing and "play" or "pause", position = cur.position,
             rate = cur.rate, playing = cur.playing }
  end
  if math.abs(cur.rate - last.rate) > 0.01 then
    return { type = "rate", rate = cur.rate, position = cur.position, playing = cur.playing }
  end
  local expected = last.position
  if last.playing then expected = expected + elapsed * last.rate end
  if math.abs(cur.position - expected) > JUMP_THRESHOLD then
    return { type = "seek", position = cur.position, rate = cur.rate, playing = cur.playing }
  end
  return nil
end

local function apply_set_state(cmd, server_now)
  local cur = read_player()
  if not cur then return false end
  local changed = false
  if type(cmd.rate) == "number" and math.abs(cur.rate - cmd.rate) > 0.01 then
    set_rate(cmd.rate)
    changed = true
  end
  if type(cmd.position) == "number" then
    local target = cmd.position
    if cmd.playing and type(cmd.at) == "number" and type(server_now) == "number" then
      target = target + (server_now - cmd.at) * (tonumber(cmd.rate) or 1)
    end
    if math.abs(cur.position - target) > SEEK_THRESHOLD then
      seek_to(target)
      changed = true
    end
  end
  if type(cmd.playing) == "boolean" and cmd.playing ~= cur.playing then
    set_playing(cmd.playing)
    changed = true
  end
  -- Reasons come from the server in English; shown as the last event in the window.
  set_msg("← " .. tostring(cmd.reason or "set_state") .. (changed and "" or (" " .. T("(already in sync)"))))
  return changed
end

local leave_local

-- Returns "ended" when the session is over, otherwise true/false (player changed or not).
local function apply_commands(cmds, server_now)
  if type(cmds) ~= "table" then return false end
  local changed = false
  for _, cmd in ipairs(cmds) do
    if type(cmd) == "table" then
      if cmd.type == "set_state" then
        if apply_set_state(cmd, server_now) then changed = true end
      elseif cmd.type == "session_ended" then
        leave_local(T("The session was ended on the server."))
        return "ended"
      end
    end
  end
  return changed
end

local function schedule_tick()
  if S.phase ~= "session" then return end
  local ok, item = pcall(vlc.input.item)
  if not ok or not item then
    set_msg(T("No media open – syncing resumes when a media is played."))
    return
  end
  S.tick_flip = not S.tick_flip
  item:set_meta(META_KEY, S.tick_flip and "play_along:1" or "play_along:0")
end

local function do_tick(slow)
  if S.phase ~= "session" then return end
  local cur = read_player()
  if not cur then
    S.last = nil
    set_msg(T("No media playing – syncing resumes when the media comes back."))
    return
  end

  local action = nil
  if S.last and type(S.last.t) == "number" and type(S.server_now) == "number"
     and os.time() >= S.grace_until then
    local elapsed = S.server_now - S.last.t
    if elapsed >= 0 and elapsed < 30 then
      action = detect_action(S.last, cur, elapsed)
    end
  end
  S.last = { playing = cur.playing, position = cur.position, rate = cur.rate, t = S.server_now }

  local resp, err = api("/api/sync", {
    code        = S.code,
    device_id   = S.device_id,
    device_name = S.device_name,
    state       = state_of(cur),
    media       = media_of(cur),
    wait_ms     = (action or not slow) and 0 or TICK_MS,
    action      = action,
  })
  if not resp then
    S.failures = S.failures + 1
    set_msg(T("Communication failure (%d/%d): %s", S.failures, MAX_FAILURES, tostring(err)))
    return
  end
  S.failures = 0
  if type(resp.server_now) == "number" then S.server_now = resp.server_now end

  if not resp.ok then
    if resp.error == "session_not_found" then
      leave_local(T("The session no longer exists on the server."))
    else
      set_msg(T("Server: %s", describe_error(resp.error)))
    end
    return
  end

  S.session_info = resp.session
  if type(resp.session) == "table" and resp.session.policy then S.policy = resp.session.policy end
  if action then
    if resp.accepted then
      set_msg(T("%s – accepted by the session", describe_action(action)))
    else
      set_msg(T("%s – rejected (%s)", describe_action(action), policy_label(S.policy)))
    end
  end

  local applied = apply_commands(resp.commands, resp.server_now)
  if applied == "ended" then return end
  if applied then
    local after = read_player()
    if after then
      S.last = { playing = after.playing, position = after.position, rate = after.rate, t = S.server_now }
    end
    S.grace_until = os.time() + GRACE_S
  end
  dbg(string.format("tick%s pos=%.1f %s %.2fx commands=%d", slow and "" or " (quick)",
                    cur.position, cur.playing and "play" or "pause", cur.rate,
                    type(resp.commands) == "table" and #resp.commands or 0))
  render_status()
end

leave_local = function(msg)
  S.phase = "idle"
  S.code = nil
  S.session_info = nil
  S.last = nil
  S.since = 0
  set_msg(msg)
end

local function enter_session(resp)
  S.phase = "session"
  if type(resp.session) == "table" then
    S.code = resp.session.code or S.code
    S.policy = resp.session.policy or S.policy
    S.session_info = resp.session
  end
  S.server_now = resp.server_now
  S.failures = 0
  S.since = 0
  S.last = nil
  S.last_tick_wall = os.time()
  if W.code and S.code then pcall(function() W.code:set_text(S.code) end) end
  local applied = apply_commands(resp.commands, resp.server_now)
  if applied == "ended" then return end
  if applied then S.grace_until = os.time() + GRACE_S end
  schedule_tick()
end

-- ===================================================================== buttons
local function on_create()
  if S.phase == "session" then
    set_msg(T("Already in session %s. Leave it before starting another.", tostring(S.code)))
    return
  end
  local cur = read_player()
  if not cur then
    warn_no_media()
    return
  end
  refresh_language()
  read_inputs()
  local policy = "anarchy"
  pcall(function() if W.policy:get_value() == 2 then policy = "monarchy" end end)
  local resp, err = api("/api/create", {
    device_id = S.device_id, device_name = S.device_name, policy = policy,
    state = state_of(cur), media = media_of(cur),
  })
  if not resp then set_msg(T("Could not start the session: %s", tostring(err))); return end
  if not resp.ok then set_msg(T("Server refused: %s", describe_error(resp.error))); return end
  enter_session(resp)
  set_msg(T("Session %s started (%s). Share the code with the other devices.",
            tostring(S.code), policy_label(S.policy)))
end

local function on_join()
  if S.phase == "session" then
    set_msg(T("Already in session %s. Leave it before joining another.", tostring(S.code)))
    return
  end
  local cur = read_player()
  if not cur then
    warn_no_media()
    return
  end
  refresh_language()
  read_inputs()
  local code = trim(W.code and W.code:get_text() or ""):upper()
  if code == "" then set_msg(T("Enter the session code.")); return end
  local resp, err = api("/api/join", {
    code = code, device_id = S.device_id, device_name = S.device_name,
    state = state_of(cur), media = media_of(cur),
  })
  if not resp then set_msg(T("Could not join: %s", tostring(err))); return end
  if not resp.ok then set_msg(T("Server refused: %s", describe_error(resp.error))); return end
  S.code = code
  enter_session(resp)
  set_msg(T("You joined session %s (%s).", tostring(S.code), policy_label(S.policy)))
end

local function on_leave()
  if S.phase ~= "session" then set_msg(T("You are not in a session.")); return end
  local code = S.code
  pcall(api, "/api/leave", { code = code, device_id = S.device_id })
  leave_local(T("You left session %s.", tostring(code)))
end

local function on_reconnect()
  if S.phase ~= "session" then set_msg(T("You are not in a session.")); return end
  read_inputs()
  S.failures = 0
  S.since = 0
  set_msg(T("Reconnecting..."))
  schedule_tick()
end

build_dialog = function(server, name, code)
  dlg = vlc.dialog("Play Along")
  dlg:add_label(T("Server (host or host:port)"), 1, 1, 1, 1)
  W.server = dlg:add_text_input(server or S.server_text, 2, 1, 3, 1)
  dlg:add_label(T("Your name"), 1, 2, 1, 1)
  W.name = dlg:add_text_input(name or S.device_name, 2, 2, 3, 1)
  dlg:add_label(T("Policy (new session)"), 1, 3, 1, 1)
  W.policy = dlg:add_dropdown(2, 3, 1, 1)
  W.policy:add_value(T("Anarchy – everyone controls"), 1)
  W.policy:add_value(T("Monarchy – only the creator controls"), 2)
  W.btn_create = dlg:add_button(T("Start session"), on_create, 3, 3, 2, 1)
  dlg:add_label(T("Session code"), 1, 4, 1, 1)
  W.code = dlg:add_text_input(code or "", 2, 4, 1, 1)
  W.btn_join = dlg:add_button(T("Join"), on_join, 3, 4, 2, 1)
  W.btn_leave = dlg:add_button(T("Leave session"), on_leave, 1, 5, 1, 1)
  W.btn_reconnect = dlg:add_button(T("Reconnect"), on_reconnect, 2, 5, 1, 1)
  W.status = dlg:add_html("", 1, 6, 4, 1)
  dlg:show()
end

-- ===================================================================== VLC callbacks
function activate()
  math.randomseed(os.time())
  S.device_name = default_name()
  load_config()
  S.lang, S.lang_source = nil, nil
  refresh_language()
  S.device_id = string.format("%s-%x-%04x", (S.device_name:gsub("[^%w]", "")), os.time(),
                              math.random(0, 0xffff))
  build_dialog()
  if read_player() then
    render_status()
  else
    warn_no_media()
  end
  log("activated (v" .. VERSION .. ", language " .. tostring(S.lang) .. " via " .. tostring(S.lang_source) .. ")")
  return true
end

function deactivate()
  if S.phase == "session" then
    pcall(api, "/api/leave", { code = S.code, device_id = S.device_id }, true)
  end
  S.phase = "idle"
  if dlg then
    pcall(function() dlg:delete() end)
    dlg = nil
  end
  log("deactivated")
  return true
end

function close()
  vlc.deactivate()
end

-- Called by VLC when an item meta changes; this is how the extension "wakes up" each cycle.
function meta_changed()
  refresh_language()
  if S.phase ~= "session" then return end
  S.since = S.since + 1
  S.last_tick_wall = os.time()
  local slow = S.since >= 2
  local ok, err = pcall(do_tick, slow)
  if not ok then set_msg(T("Internal error: %s", tostring(err))) end
  if S.phase ~= "session" then return end
  if slow then
    S.since = 0
    if S.failures < MAX_FAILURES then
      schedule_tick()
    else
      set_msg(T("No connection to the server. Click Reconnect (or play/pause) to retry."))
    end
  end
end

function input_changed()
  refresh_language()
  if S.phase ~= "session" then
    if S.no_media_warning and read_player() then
      S.no_media_warning = false
      S.last_msg = ""
      render_status()
    end
    return
  end
  S.last = nil
  S.grace_until = os.time() + GRACE_S
  S.since = 0
  schedule_tick()
end

function playing_changed(status)
  refresh_language()
  if S.phase ~= "session" then return end
  -- If the cycle chain stopped (network failures), a user play/pause restarts it.
  if os.time() - (S.last_tick_wall or 0) > 5 then
    S.since = 0
    S.failures = 0
    set_msg(T("Resuming sync..."))
    schedule_tick()
  end
end
