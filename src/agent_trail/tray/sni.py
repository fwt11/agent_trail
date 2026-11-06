"""原生 StatusNotifierItem（SNI）+ DBusMenu 托盘实现（Gio/GDBus 版）。

Deepin 23 的 dde-session-dae 托盘与 libayatana-appindicator 不兼容（不发起
注册交互），而系统预装的 dbus-python 1.4.0 又缺少 Variant 类型（逐属性 Get
必失败，宿主查询后放弃渲染）。因此本模块改用 PyGObject 自带的 Gio/GDBus：

- bus_own_name 持有 org.kde.StatusNotifierItem-<pid>-1
- register_object 导出 /StatusNotifierItem（属性由 Gio 自动响应
  org.freedesktop.DBus.Properties.Get/GetAll）
- /StatusNotifierMenu 实现 com.canonical.dbusmenu（Version 3）
- 以「总线名」调用 watcher 的 RegisterStatusNotifierItem（dde 按
  总线名/对象路径 记录条目；传对象路径会被记成唯一名而不渲染）

菜单事件回调在 GLib 主线程触发（与 Gtk 同循环）。
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gio, GLib

ITEM_PATH = "/StatusNotifierItem"
MENU_PATH = "/StatusNotifierMenu"
ITEM_BUS_PREFIX = "org.kde.StatusNotifierItem"

_ITEM_XML = """
<node>
  <interface name="org.kde.StatusNotifierItem">
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="WindowId" type="i" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="IconPixmap" type="a(iiay)" access="read"/>
    <property name="OverlayIconName" type="s" access="read"/>
    <property name="OverlayIconPixmap" type="a(iiay)" access="read"/>
    <property name="AttentionIconName" type="s" access="read"/>
    <property name="AttentionIconPixmap" type="a(iiay)" access="read"/>
    <property name="AttentionMovieName" type="s" access="read"/>
    <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
    <property name="Menu" type="o" access="read"/>
    <method name="Activate">
      <arg name="x" type="i" direction="in"/>
      <arg name="y" type="i" direction="in"/>
    </method>
    <method name="SecondaryActivate">
      <arg name="x" type="i" direction="in"/>
      <arg name="y" type="i" direction="in"/>
    </method>
    <method name="Scroll">
      <arg name="delta" type="i" direction="in"/>
      <arg name="orientation" type="s" direction="in"/>
    </method>
  </interface>
</node>
"""

_MENU_XML = """
<node>
  <interface name="com.canonical.dbusmenu">
    <property name="Version" type="u" access="read"/>
    <property name="TextDirection" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconThemePath" type="as" access="read"/>
    <method name="AboutToShow">
      <arg name="id" type="i" direction="in"/>
      <arg name="needUpdate" type="b" direction="out"/>
    </method>
    <method name="GetLayout">
      <arg name="parentId" type="i" direction="in"/>
      <arg name="recursionDepth" type="i" direction="in"/>
      <arg name="propertyNames" type="as" direction="in"/>
      <arg name="revision" type="u" direction="out"/>
      <arg name="layout" type="(ia{sv}av)" direction="out"/>
    </method>
    <method name="GetGroupProperties">
      <arg name="ids" type="ai" direction="in"/>
      <arg name="propertyNames" type="as" direction="in"/>
      <arg name="properties" type="a(ia{sv})" direction="out"/>
    </method>
    <method name="Event">
      <arg name="id" type="i" direction="in"/>
      <arg name="eventId" type="s" direction="in"/>
      <arg name="data" type="v" direction="in"/>
      <arg name="timestamp" type="u" direction="in"/>
    </method>
    <signal name="ItemsPropertiesUpdated">
      <arg name="revision" type="u" direction="out"/>
      <arg name="updatedProps" type="a(ia{sv})" direction="out"/>
      <arg name="removedIds" type="as" direction="out"/>
    </signal>
  </interface>
</node>
"""


def _log(message: str) -> None:
    """DBus 方法调用日志走 stderr（stdout 是 JSON 事件通道）。"""
    thread = threading.current_thread().name
    print(f"[sni:{thread}] {message}", file=sys.stderr, flush=True)


def load_icon_argb(icon_path: Path) -> tuple[int, int, bytes]:
    """把 PNG 解码成 SNI 的 IconPixmap 数据 (w, h, ARGB 字节序列)。

    用 GDK 的 GdkPixbuf 加载（GTK 生态必带），避免对 Pillow 的隐式依赖。
    """
    from gi.repository import GdkPixbuf

    pixbuf = GdkPixbuf.Pixbuf.new_from_file(str(icon_path))
    if pixbuf.get_n_channels() != 4 or not pixbuf.get_has_alpha():
        converted = pixbuf.add_alpha(False, 0, 0, 0)
        if converted is not None:
            pixbuf = converted
    width = pixbuf.get_width()
    height = pixbuf.get_height()
    rowstride = pixbuf.get_rowstride()
    raw = pixbuf.get_pixels()
    w4 = width * 4
    argb = bytearray(height * w4)
    for y in range(height):
        row = raw[y * rowstride : y * rowstride + w4]
        seg = bytearray(w4)
        seg[0::4] = row[3::4]  # A
        seg[1::4] = row[0::4]  # R
        seg[2::4] = row[1::4]  # G
        seg[3::4] = row[2::4]  # B
        argb[y * w4 : (y + 1) * w4] = seg
    return width, height, bytes(argb)


class MenuSpec:
    """一条菜单项的声明；id 由 SniTray 统一分配。"""

    __slots__ = ("label", "enabled", "is_separator", "is_check", "checked", "action")

    def __init__(self, label="", *, enabled=True, is_separator=False,
                 is_check=False, checked=False, action=None) -> None:
        self.label = label
        self.enabled = enabled
        self.is_separator = is_separator
        self.is_check = is_check
        self.checked = checked
        self.action = action


class SniTray:
    def __init__(self, title: str, tooltip: str, icon_path: Path) -> None:
        self._title = title
        self._tooltip = tooltip
        self._icon = load_icon_argb(icon_path)
        self._specs: list[MenuSpec] = []
        self._revision = 0
        self._registrations: list[int] = []
        self._owner_id = 0
        self._connection: Gio.DBusConnection | None = None
        self._watcher: str | None = None
        self._activate_cb = None
        self.bus_name = f"{ITEM_BUS_PREFIX}-{os.getpid()}-1"
        self._item_info = Gio.DBusNodeInfo.new_for_xml(_ITEM_XML)
        self._menu_info = Gio.DBusNodeInfo.new_for_xml(_MENU_XML)

        self._owner_id = Gio.bus_own_name(
            Gio.BusType.SESSION,
            self.bus_name,
            Gio.BusNameOwnerFlags.NONE,
            self._on_bus_acquired,
            self._on_name_acquired,
            self._on_name_lost,
        )

    # -- 名称所有权 ----------------------------------------------------------
    def _on_bus_acquired(self, connection: Gio.DBusConnection, name: str) -> None:
        self._connection = connection
        self._registrations.append(
            connection.register_object(
                ITEM_PATH,
                self._item_info.lookup_interface("org.kde.StatusNotifierItem"),
                self._item_method_call,
                self._item_get_property,
                None,
            )
        )
        self._registrations.append(
            connection.register_object(
                MENU_PATH,
                self._menu_info.lookup_interface("com.canonical.dbusmenu"),
                self._menu_method_call,
                self._menu_get_property,
                None,
            )
        )

    def _on_name_acquired(self, connection: Gio.DBusConnection, name: str) -> None:
        self._register_watcher(connection)

    def _on_name_lost(self, connection, name: str) -> None:
        _log(f"name lost: {name}")

    def _register_watcher(self, connection: Gio.DBusConnection) -> None:
        # dde-session-dae 的 watcher 按「总线名/对象路径」记录条目：参数必须
        # 传总线名，传对象路径会被记成唯一名，宿主不渲染。
        for watcher in ("org.kde.StatusNotifierWatcher",
                        "org.freedesktop.StatusNotifierWatcher"):
            try:
                connection.call_sync(
                    watcher,
                    "/StatusNotifierWatcher",
                    "org.kde.StatusNotifierWatcher",
                    "RegisterStatusNotifierItem",
                    GLib.Variant("(s)", [self.bus_name]),
                    None,
                    Gio.DBusCallFlags.NONE,
                    2000,
                    None,
                )
                self._watcher = watcher
                _log(f"registered via {watcher}")
                return
            except Exception as error:
                _log(f"register via {watcher} failed: {type(error).__name__}")
        self._watcher = None

    # -- SNI 对象 ------------------------------------------------------------
    def _item_get_property(self, connection, sender, path, interface, prop):
        if prop == "Category":
            return GLib.Variant("s", "ApplicationStatus")
        if prop == "Id":
            return GLib.Variant("s", "agent-trail")
        if prop == "Title":
            return GLib.Variant("s", self._title)
        if prop == "Status":
            return GLib.Variant("s", "Active")
        if prop == "WindowId":
            return GLib.Variant("i", 0)
        if prop == "IconName":
            return GLib.Variant("s", "")
        if prop == "IconPixmap":
            width, height, argb = self._icon
            return GLib.Variant("a(iiay)", [(width, height, argb)])
        if prop in ("OverlayIconName", "AttentionIconName", "AttentionMovieName"):
            return GLib.Variant("s", "")
        if prop in ("OverlayIconPixmap", "AttentionIconPixmap"):
            return GLib.Variant("a(iiay)", [])
        if prop == "ToolTip":
            return GLib.Variant("(sa(iiay)ss)", ("", [], self._title, self._tooltip))
        if prop == "Menu":
            return GLib.Variant("o", MENU_PATH)
        raise GLib.Error(f"unknown property {prop}")

    def _item_method_call(self, connection, sender, path, interface, method, params, invocation):
        if method in ("Activate", "SecondaryActivate"):
            _log(f"{method} {params.unpack()}")
            if self._activate_cb is not None:
                self._activate_cb()
            invocation.return_value(None)
            return
        if method == "Scroll":
            invocation.return_value(None)
            return
        _log(f"unknown item method {method}")
        invocation.return_value(None)

    # -- 菜单对象 -------------------------------------------------------------
    def _menu_get_property(self, connection, sender, path, interface, prop):
        if prop == "Version":
            return GLib.Variant("u", 3)
        if prop == "TextDirection":
            return GLib.Variant("s", "ltr")
        if prop == "Status":
            return GLib.Variant("s", "normal")
        if prop == "IconThemePath":
            return GLib.Variant("as", [])
        raise GLib.Error(f"unknown property {prop}")

    def _menu_item_props(self, item_id: int) -> dict:
        spec = self._specs[item_id - 1]
        props: dict = {
            "label": GLib.Variant("s", spec.label),
            "enabled": GLib.Variant("b", spec.enabled),
            "visible": GLib.Variant("b", True),
        }
        if spec.is_separator:
            props["type"] = GLib.Variant("s", "separator")
        if spec.is_check:
            props["toggle-type"] = GLib.Variant("s", "checkmark")
            props["toggle-state"] = GLib.Variant("i", 1 if spec.checked else 0)
        return props

    def _menu_method_call(self, connection, sender, path, interface, method, params, invocation):
        args = params.unpack()
        if method == "GetLayout":
            parent, _depth, _names = args
            _log(f"GetLayout(parent={parent})")
            children = []
            if parent == 0:
                for item_id in range(1, len(self._specs) + 1):
                    spec = self._specs[item_id - 1]
                    if spec.is_separator:
                        continue
                    children.append(GLib.Variant(
                        "(ia{sv}av)",
                        (item_id, self._menu_item_props(item_id), []),
                    ))
            # 注意：结构体槽位上传原始元组（预构建 Variant 会被 PyGObject
            # 覆盖层错误下钻），av 槽放 Variant 列表。
            invocation.return_value(GLib.Variant(
                "(u(ia{sv}av))",
                (self._revision,
                 (0,
                  {"children-display": GLib.Variant("s", "submenu")},
                  children)),
            ))
            return
        if method == "AboutToShow":
            invocation.return_value(GLib.Variant("(b)", (False,)))
            return
        if method == "GetGroupProperties":
            ids = args[0]
            out = []
            for item_id in ids:
                if 1 <= item_id <= len(self._specs):
                    out.append((item_id, self._menu_item_props(item_id)))
            invocation.return_value(GLib.Variant("(a(ia{sv}))", (out,)))
            return
        if method == "Event":
            item_id, event, _data, _ts = args
            _log(f"Event(id={item_id}, event={event})")
            if event == "clicked" and 1 <= item_id <= len(self._specs):
                spec = self._specs[item_id - 1]
                if spec.action is not None:
                    spec.action()
            invocation.return_value(None)
            return
        _log(f"unknown menu method {method}")
        invocation.return_value(None)

    # -- 对外 API -------------------------------------------------------------
    def set_menu(self, specs: list[MenuSpec]) -> None:
        self._specs = specs
        self._revision += 1
        if self._connection is None:
            return
        updated = [
            (item_id, self._menu_item_props(item_id))
            for item_id in range(1, len(specs) + 1)
        ]
        try:
            self._connection.emit_signal(
                None,
                MENU_PATH,
                "com.canonical.dbusmenu",
                "ItemsPropertiesUpdated",
                GLib.Variant("(ua(ia{sv})as)", (self._revision, updated, [])),
            )
        except Exception:
            pass

    def bind_activate(self, callback) -> None:
        self._activate_cb = callback

    @property
    def registered(self) -> bool:
        return self._watcher is not None

    def close(self) -> None:
        if self._connection is not None:
            for reg_id in self._registrations:
                try:
                    self._connection.unregister_object(reg_id)
                except Exception:
                    pass
            self._registrations = []
        if self._owner_id:
            Gio.bus_unown_name(self._owner_id)
            self._owner_id = 0
