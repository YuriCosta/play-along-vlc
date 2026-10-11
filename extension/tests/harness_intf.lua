-- harness_intf.lua: runs the Play Along extension logic inside a VLC WITHOUT a GUI, as a
-- "lua intf" (vlc -I luaintf --lua-intf=pa_test ...). Driven by headless_test.py.
--
-- Environment variables:
--   PA_EXT      path to play_along.lua
--   PA_WORKDIR  folder holding pa_cmd.txt (commands for the harness) and pa_state.txt (state)
--   PA_SERVER   server text typed into the window (default 127.0.0.1:8765)
local WORKDIR = ((os.getenv("PA_WORKDIR") or "."):gsub("\\", "/")) .. "/"
local EXT = ((os.getenv("PA_EXT") or "play_along.lua"):gsub("\\", "/"))
local SERVER = os.getenv("PA_SERVER") or "127.0.0.1:8765"
local CMD_FILE, STATE_FILE = WORKDIR .. "pa_cmd.txt", WORKDIR .. "pa_state.txt"

HARNESS = { buttons = {}, inputs = {}, pending = 0 }

local function read_file(p)
  local f = io.open(p, "rb"); if not f then return nil end
  local s = f:read("*a"); f:close(); return s
end
local function write_file(p, s)
  local f = io.open(p, "wb"); if f then f:write(s); f:close() end
end

local function widget(text)
  local w = { text = text or "" }
  function w:set_text(t) self.text = t end
  function w:get_text() return self.text end
  function w:add_value(t, id) self.value = self.value or id end
  function w:get_value() return self.value end
  return w
end

-- vlc.dialog does not exist in intf scripts: stub that captures the extension's widgets
vlc.dialog = function(title)
  local d = {}
  function d:add_label(t) return widget(t) end
  function d:add_text_input(t) local w = widget(t); table.insert(HARNESS.inputs, w); return w end
  function d:add_dropdown() local w = widget(""); HARNESS.policy = w; return w end
  function d:add_button(t, cb) local w = widget(t); HARNESS.buttons[t] = cb; return w end
  function d:add_html(t) local w = widget(t); HARNESS.status = w; return w end
  function d:show() end
  function d:hide() end
  function d:delete() end
  function d:update() end
  return d
end
vlc.deactivate = vlc.deactivate or function() end

-- Emulates the VLC 3 Qt interface: every item:set_meta yields TWO meta_changed()
local real_item = vlc.input.item
vlc.input.item = function()
  local it = real_item()
  if not it then return nil end
  return setmetatable({}, { __index = function(_, k)
    if k == "set_meta" then
      return function(_, key, val)
        HARNESS.pending = HARNESS.pending + 2
        return it:set_meta(key, val)
      end
    end
    local v = it[k]
    if type(v) == "function" then return function(_, ...) return v(it, ...) end end
    return v
  end })
end

dofile(EXT)

local function state_line()
  local input = vlc.object.input()
  local pos, rate = -1, 1
  if input then
    pos = (tonumber(vlc.var.get(input, "time")) or 0) / 1e6
    rate = tonumber(vlc.var.get(input, "rate")) or 1
  end
  local html = ((HARNESS.status and HARNESS.status.text) or ""):gsub("[\r\n|]", " ")
  local code = (HARNESS.inputs[3] and HARNESS.inputs[3].text) or ""
  return string.format("%s|%.2f|%.2f|%s|%s", vlc.playlist.status(), pos, rate, code, html)
end

local function run_cmd(cmd)
  local name, arg = cmd:match("^(%S+)%s*(.-)%s*$")
  vlc.msg.info("[harness] cmd: " .. tostring(cmd))
  local input = vlc.object.input()
  if name == "create" then
    HARNESS.policy.value = (arg == "monarchy") and 2 or 1
    HARNESS.buttons["Start session"]()
  elseif name == "join" then
    HARNESS.inputs[3].text = arg
    HARNESS.buttons["Join"]()
  elseif name == "leave" then HARNESS.buttons["Leave session"]()
  elseif name == "reconnect" then HARNESS.buttons["Reconnect"]()
  elseif name == "seek" and input then vlc.var.set(input, "time", math.floor(tonumber(arg) * 1e6))
  elseif name == "pause" and input then vlc.var.set(input, "state", 3)
  elseif name == "play" and input then vlc.var.set(input, "state", 2)
  elseif name == "rate" and input then vlc.var.set(input, "rate", tonumber(arg))
  elseif name == "quit" then return "quit"
  end
end

local function should_die()
  local f = vlc.misc and vlc.misc.should_die
  if f then return f() end
  return false
end

vlc.msg.info("[harness] starting (ext=" .. EXT .. ", workdir=" .. WORKDIR .. ")")
activate()
HARNESS.inputs[1].text = SERVER
HARNESS.inputs[2].text = "VLC-test"

local quit = false
local had_input = false
while not quit and not should_die() do
  local ok, err = pcall(function()
    -- Emulates the input_changed() the Qt interface fires when the media changes/appears
    local has_input = vlc.object.input() ~= nil
    if has_input ~= had_input then
      had_input = has_input
      input_changed()
    end
    local cmd = read_file(CMD_FILE)
    if cmd and cmd:match("%S") then
      write_file(CMD_FILE, "")
      if run_cmd(cmd) == "quit" then quit = true end
    end
    local n = 0
    while HARNESS.pending > 0 and n < 2 do
      HARNESS.pending = HARNESS.pending - 1
      n = n + 1
      meta_changed()
    end
    write_file(STATE_FILE, state_line())
  end)
  if not ok then vlc.msg.err("[harness] error: " .. tostring(err)) end
  vlc.misc.mwait(vlc.misc.mdate() + 200000)
end
vlc.msg.info("[harness] done")
deactivate()
vlc.misc.quit()
