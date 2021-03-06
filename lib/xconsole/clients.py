#!/usr/bin/env python2
# coding: utf-8


from __future__ import absolute_import
from __future__ import print_function

import sys
sys.dont_write_bytecode = True

import re
import array
import struct
import operator
import cStringIO

from pprint import (
    pformat as pf,
    pprint as pp,
    )

import mapo
import time
from datetime import datetime

import logging
logging.basicConfig()
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
#logger.setLevel(logging.DEBUG)
logger.info(
    '\n%s\n%16s: init: %s\n%s', *(
        '-'*64, __name__, datetime.now(), '-'*64,
        ))


class _Repr(object):

    def __repr__(self, other=None):
        try:
            ident = self.ident
        except AttributeError:
            ident = hex(id(self))
        keys = [] and [    #XXX
            k for k in vars(self)
            if not k.startswith(('len_', 'num_', '__'))
                and not k.endswith(('_len', '_num'))
            ]
        eol = '(' if keys else '>'
        fmt = (
            '<{self.__class__.__module__}'
            '.{self.__class__.__name__}'
            ':{ident}{eol}'
            ).format(self=self, ident=ident, eol=eol)

        if keys:
            fmt = [fmt]
            tbc = '.' * max(2, *map(len, keys))
            for k in sorted(keys):
                attr = getattr(self, k)
                if k in ('window',):
                    attr = xid(attr)
                if isinstance(attr, xcb.List):
                    attr = list(attr)
                    if k in ('name',):
                        attr = ''.join(map(chr, attr))
                fmt.append('{0} {1} {2}'.format(k, tbc[len(k)-2:], attr))
            fmt.append(')>')
            fmt = '\n  '.join(fmt)

        return fmt


class _Identifier(_Repr):

    @property
    def atom(self):
        atom = self.__dict__.get('atom')
        if atom is not None:
            return atom

        atom = self.__dict__['atom'] = mapo.record()
        return atom

    @property
    def ident(self, counter=__import__('collections').Counter()):
        ident = self.atom.get('IDENT')
        if ident is not None:
            return ident

        ident = self.atom.IDENT = counter[self.__class__]
        counter[self.__class__] += 1
        return ident


# (reverse) http://stackoverflow.com/questions/8638792
def FP1616(v):
    return v * 65536.0


class xid(int):

    def __str__(self):
        return str(hex(self))


class Manager(_Identifier, object):

    def __init__(self, *args, **kwds):
        self.title_map = mapo.record()
        self.event_map = mapo.record()
        self.window_map = mapo.record()
        self.device_map = mapo.record()
        self.controller_map = mapo.record()
        self.connection = None

    @property
    def conn(self):
        if self.connection:
            return self.connection

        conn = self.connection = xcb.connect()
        conn.randr = conn(randr.key)
        conn.render = conn(render.key)
        conn.xfixes = conn(xfixes.key)
        conn.xinput = conn(xinput.key)

        conn.randr.QueryVersion(1, 4).reply()
        conn.render.QueryVersion(0, 11).reply()
        conn.xfixes.QueryVersion(5, 0).reply()
        conn.xinput.XIQueryVersion(2, 3).reply()

        self.root = conn.get_setup().roots[0]

        return self.connection

    def sink_events(self):
        ximask = (
            xinput.XIEventMask.RawKeyPress
            | xinput.XIEventMask.RawKeyRelease
            | xinput.XIEventMask.RawButtonPress
            | xinput.XIEventMask.RawButtonRelease
            )
        self.conn.xinput.XISelectEvents(
            self.root.root, [
                (device.deviceid, ximask)
                for device in self.device_map.values()
                if device.type not in (
                    xinput.DeviceType.MasterPointer,
                    xinput.DeviceType.MasterKeyboard,
                    )
                ])
        self.conn.xinput.XISelectEvents(
            self.root.root, [
                (xinput.Device.All, xinput.XIEventMask.Hierarchy),
                ])

        self.conn.core.ChangeWindowAttributesChecked(
            self.root.root, xproto.CW.EventMask, [
                xproto.EventMask.SubstructureRedirect |
                xproto.EventMask.SubstructureNotify
                ]
            ).check()

        import os
        name = 'xconsole:{0}'.format(os.getpid())
        self.conn.core.ChangePropertyChecked(
            xproto.PropMode.Replace, self.root.root,
            xproto.Atom.WM_NAME, xproto.Atom.STRING,
            8, len(name), name,
            ).check()

    def refresh_devices(self):
        SOL = object()
        stack = list(
            ((xid(info.deviceid), info), self.device_map)
            for info in self.conn.xinput.XIQueryDevice(0).reply().infos
            )

        existing = self.device_map.copy()
        while stack:
            #...reverse/depth-first allows for list deletes
            (key, attr), node = stack.pop()

            if attr is SOL:
                node.pop(key)
                continue

            if key == 'name':
                attr = ''.join(map(chr, attr)).strip(' \t\n\r\0')
            elif key == 'classes':
                attr = set(vc.type for vc in attr)
            elif key == 'deviceid':
                attr = xid(attr)
            elif hasattr(key, 'endswith'):
                if (key in ('len', 'uninterpreted_data') or
                    key.startswith(('len_', 'num_')) or
                    key.endswith(('_len', '_num'))):
                    attr = SOL

            loop = tuple()
            if attr is SOL:
                stack.append(((key, attr), node))
            elif isinstance(attr, xcb.List):
                attr = list(attr)
                loop = enumerate(attr)
            elif isinstance(attr, xcb.Struct):
                attr = mapo.record(vars(attr))
                loop = attr.iteritems()

            stack.extend((kv, attr) for kv in loop)
            node[key] = attr

        return self.device_map - existing

    def on_xge(self, event):
        eventmap = {
            1: 'on_device_changed',
            11: 'on_hierarchy_changed',
            13: 'on_raw_key_press',
            14: 'on_raw_key_release',
            15: 'on_raw_button_press',
            16: 'on_raw_button_release',
            }
        if event.xgevent not in eventmap:
            return

        attr = eventmap[event.xgevent]

        if event.xgevent == 11:
            self.refresh_devices()
            for controller in set(self.controller_map.values()):
                handler = getattr(controller, attr, None)
                if handler:
                    handler(event)
            return None

        device = self.device_map[event.deviceid]
        if not attr.startswith('on_raw_') and hasattr(event, 'sourceid'):
            device = self.device_map[event.sourceid]
        key = (device.deviceid, 0)
        if 1 in device.classes:
            key = tuple(reversed(key))
        controller = self.next_controller(key)
        handler = getattr(controller, attr, None)
        if handler:
            return handler(event)

    def next_controller(self, key):
        last_controller = self.controller_map.get((0, 0))
        next_controller = self.controller_map.get(key)
        if key[0] == 0:
            return last_controller

        if not next_controller:
            next_controller = Controller(self, key)
        if last_controller != next_controller:
            if last_controller:
                last_controller.on_focus_out(next_controller)
            next_controller.on_focus_in(last_controller)
        if next_controller:
            self.controller_map[(0, 0)] = next_controller
        return next_controller

    def get_port(self, event=None, controller=None):
        logger.info(self.controller_map)
        if event is not None:
            port = self.window_map.get(
                getattr(event, 'window', None),
                )
            if port is not None:
                return port

        if controller is None:
            avail = sorted(
                (c.ident, c)
                for c in set(self.controller_map.values())
                    if 'FRAME' not in c.port.atom
                )
            if avail:
                return avail[0][1].port

        return None

    def create_cursor(self):
        fid = xid(self.conn.generate_id())
        cid = self.atom.CURSOR = xid(self.conn.generate_id())
        self.conn.core.OpenFontChecked(
            fid, 6,'cursor',
            ).check()
        self.conn.core.CreateGlyphCursorChecked(
            cid, fid, fid, 30, 30, 0, 0, 0, 0, 0, 0,
            ).check()
        self.conn.core.ChangeWindowAttributesChecked(
            self.root.root, xproto.CW.Cursor, [cid],
            ).check()
        self.conn.core.CloseFontChecked(fid).check()

    def main_loop(self):
        self.refresh_devices()
        self.sink_events()
        self.create_cursor()

        while True:
            try:
                self.conn.flush()
                event = self.conn.wait_for_event()
            except xcb.ProtocolException as e:
                logger.exception(e)
                continue

            except KeyboardInterrupt:
                break

            else:
                logger.debug(
                    '%s:\n%s',
                    event.__class__.__name__,
                    pf(vars(event), width=1),
                    )

            #from IPython import embed as I; I()

            if isinstance(event, xproto.GeGenericEvent):
                self.on_xge(event)
            elif isinstance(event, xproto.MapRequestEvent):
                self.conn.core.MapWindowChecked(event.window).check()
                port = self.window_map.get(event.window)
                if port:
                    port.on_map_request(event)
            elif isinstance(event, xproto.ConfigureRequestEvent):
                port = self.get_port(event)
                if port:
                    event.value_mask |= (
                        xproto.ConfigWindow.X |
                        xproto.ConfigWindow.Y |
                        xproto.ConfigWindow.Width |
                        xproto.ConfigWindow.Height
                        )
                    event.x, event.y = port.atom.POS
                    event.width, event.height = port.atom.DIM
                    if event.window not in port.atom.WID:
                        port.window = event.window
                    if 'FRAME' not in port.atom:
                        port.window = port.frame
                        self.conn.core.CreateWindowChecked(
                            self.root.root_depth,
                            port.window,
                            self.root.root,
                            port.atom.POS[0],
                            port.atom.POS[1],
                            port.atom.DIM[0],
                            port.atom.DIM[1],
                            0,
                            xproto.WindowClass.InputOutput,
                            self.root.root_visual,
                            0, [],
                            ).check()
                        self.conn.core.ChangePropertyChecked(
                            xproto.PropMode.Replace,
                            port.window,
                            xproto.Atom.WM_NAME,
                            xproto.Atom.STRING,
                            8, len(port.controller.atom.NAME),
                            port.controller.atom.NAME,
                            ).check()
                        self.conn.core.MapWindowChecked(
                            port.window,
                            ).check()
                        self.conn.core.ReparentWindowChecked(
                            event.window,
                            port.window,
                            0, 0,
                            ).check()
                    event = port.on_configure_request(event)
                if event.border_width > 0:
                    event.value_mask |= xproto.ConfigWindow.BorderWidth
                    event.border_width = 0
                self.conn.core.ConfigureWindowChecked(
                    event.window, event.value_mask, list(
                        getattr(event, key)
                        for key in (
                            'x',
                            'y',
                            'width',
                            'height',
                            'border_width',
                            'sibling',
                            'stack_mode',
                            )
                        if event.value_mask & getattr(
                            xproto.ConfigWindow,
                            key.title().replace('_', ''),
                            )
                        )).check()

        self.conn.disconnect()


class Controller(_Identifier, object):

    def __init__(self, manager, key=None):
        self.manager = manager
        self.keym = None
        self.key = key
        self.keycodes = mapo.record(
            need = {37, 50},
            want = {37, 50},
            )

        self.atom.NAME = 'xconsole:{}'.format(self.ident)
        self.atom.PORT = Port(manager=manager, controller=self)

    @property
    def port(self):
        return self.atom.get('PORT')

    @port.setter
    def port(self, new):
        old = self.atom.get('PORT')
        if old is new:
            return

        self.atom['PORT'] = new

    @property
    def keym(self):
        if self._key[1] == 0:
            return None

        if self.atom & {'MKBD', 'MPTR'}:
            return self.atom.MKBD, self.atom.MPTR

        logger.info('@keym: %s', self)
        changes = self.manager.refresh_devices()
        self.manager.conn.xinput.XIChangeHierarchyChecked([(
            xinput.HierarchyChangeType.AddMaster,
            1, # send_core
            1, # enable
            #TODO: ^^^ disable by default?
            self.atom.NAME,
            )]).check()

        changes = self.manager.refresh_devices()
        for device in changes.values():
            if device.type == xinput.DeviceType.MasterKeyboard:
                self.atom.MKBD = device.deviceid
            elif device.type == xinput.DeviceType.MasterPointer:
                self.atom.MPTR = device.deviceid

        self._attach_devices()
        changes = self.manager.refresh_devices()
        return self.atom.MKBD, self.atom.MPTR

    def _attach_devices(self):
        logger.info('_attach_devices: %s', self)
        self.manager.conn.xinput.XIChangeHierarchyChecked([
            (xinput.HierarchyChangeType.AttachSlave,
             self._key[0], self.atom.MKBD),
            (xinput.HierarchyChangeType.AttachSlave,
             self._key[1], self.atom.MPTR),
            ]).check()
        self.manager.conn.xinput.XISelectEvents(
            self.manager.root.root, [
                (self.atom.MKBD, xinput.XIEventMask.DeviceChanged),
                (self.atom.MPTR, xinput.XIEventMask.DeviceChanged),
                ])

    @keym.setter
    def keym(self, k):
        if not k:
            return

        self.atom.MKBD, self.atom.MPTR = tuple(map(int, k))

    @property
    def key(self):
        return self._key

    @key.setter
    def key(self, k):
        if not k:
            return

        k = self._key = tuple(map(int, k))
        for alt_k in (k, (k[0], 0), (0, k[1])):
            if sum(alt_k) > 0:
                self.manager.controller_map[alt_k] = self

        if 0 not in k:
            self.unsink_events()

    @property
    def atoms(self):
        return '|'.join(map(str, sorted(self.atom.iteritems())))

    def unsink_events(self):
        self.manager.conn.xinput.XISelectEvents(
            self.manager.root.root,
            zip(self._key, (0, 0)),
            )

    def on_hierarchy_changed(self, event):
        if not event.flags & xinput.HierarchyMask.DeviceEnabled:
            return None

        if not self.keym:
            return None

        if (self.skbd.attachment != self.mkbd.deviceid
            or self.sptr.attachment != self.mptr.deviceid):
            logger.info('on_hierarchy_changed: %s', self)
            self._attach_devices()

    @property
    def mkbd(self):
        mkbd = self.manager.device_map[self.keym[0]]
        return mkbd

    @property
    def mptr(self):
        mptr = self.manager.device_map[self.keym[1]]
        return mptr

    @property
    def skbd(self):
        skbd = self.manager.device_map[self._key[0]]
        return skbd

    @property
    def sptr(self):
        sptr = self.manager.device_map[self._key[1]]
        return sptr

    def on_raw_key_press(self, event):
        logger.info(
            'on_raw_key_press: %s %s',
            self, event.detail,
            )
        self.keycodes.want -= {event.detail}

    def on_raw_key_release(self, event):
        logger.info(
            'on_raw_key_release: %s %s',
            self, event.detail,
            )
        if event.detail in self.keycodes.need:
            self.keycodes.want |= {event.detail}

    def on_raw_button_press(self, event):
        logger.info(
            'on_raw_button_press: %s %s %s',
            self, event.detail, event.deviceid,
            )
        if self.key[1] == 0 and not self.keycodes.want:
            self.key = (self.key[0], event.deviceid)
            self.atom.PAIRED = True
            logger.info('paired: %s', self)
            from .title import minecraft
            title = self.atom.TITLE = minecraft.get(self)
            logger.info('starting: %s %s', self, title)
            title.start()
            self.atom.STARTED = True
            logger.info('started: %s', self)

    def on_raw_button_release(self, event):
        logger.info(
            'on_raw_button_release: %s %s %s',
            self, event.detail, event.deviceid,
            )

    def on_focus_in(self, last_controller=None):
        logger.info('on_focus_in: %s', self)
        self.keycodes.want |= self.keycodes.need

    def on_focus_out(self, next_controller=None):
        logger.info('on_focus_out: %s', self)
        self.keycodes.want |= self.keycodes.need


class Port(_Identifier, object):

    def __init__(self, manager, controller=None, wid=None):
        self.manager = manager
        self.controller = controller
        self.atom.WID = list()
        self.window = wid

        #FIXME
        x = y = 0
        w = manager.root.width_in_pixels/2
        h = manager.root.height_in_pixels
        if self.ident > 0:
            x = w - 3
            w = w + 3
        else:
            w = w - 3

        self.atom.POS = (x, y)
        self.atom.DIM = (w, h)

    @property
    def frame(self):
        frame = self.atom.get('FRAME')
        if frame is not None:
            return frame

        frame = self.atom.FRAME = xid(self.conn.generate_id())
        return frame

    @property
    def atoms(self):
        return '|'.join(map(str, sorted(self.atom.iteritems())))

    @property
    def x(self):
        return self.atom.get('POS', (None, None))[0]

    @property
    def x(self):
        return self.atom.get('POS', (None, None))[0]

    @property
    def w(self):
        return self.atom.get('DIM', (None, None))[0]

    @property
    def h(self):
        return self.atom.get('DIM', (None, None))[1]

    @property
    def conn(self):
        return self.manager.conn

    @property
    def window(self):
        return self.atom.WID[-1] if self.atom.WID else None

    @window.setter
    def window(self, wid):
        if not wid:
            #TODO: handle set to None (remove from maps)
            return

        wid = xid(wid)
        self.atom.WID.append(wid)
        self.manager.window_map[wid] = self

    def on_configure_request(self, event):
        logger.info('on_configure_request: %s', self)
        return event

    def on_map_request(self, event):
        logger.info('on_map_request: %s', self)
        #self._set_window_attributes()
        self._on_configure_window()
        self._set_client_pointer()
        self._set_barrier()
        self._set_pointer()
        self._set_focus()

    def _on_configure_window(self):
        logger.info('_on_configure_window: %s', self)
        self.conn.core.ConfigureWindowChecked(
            self.window, (
                xproto.ConfigWindow.X
                | xproto.ConfigWindow.Y
                | xproto.ConfigWindow.Width
                | xproto.ConfigWindow.Height
                ),
            self.atom.POS + self.atom.DIM,
            ).check()
        self.conn.core.ConfigureWindowChecked(
            self.atom.WID[0], (
                xproto.ConfigWindow.X
                | xproto.ConfigWindow.Y
                | xproto.ConfigWindow.Width
                | xproto.ConfigWindow.Height
                ),
            (0, 0) + self.atom.DIM,
            ).check()

    def _set_window_attributes(self):
        logger.info('_set_window_attributes: %s', self)
        mask = (
            xproto.EventMask.EnterWindow |
            xproto.EventMask.LeaveWindow |
            xproto.EventMask.FocusChange
            )
        self.manager.conn.core.ChangeWindowAttributesChecked(
            self.window, xproto.CW.EventMask, [mask],
            ).check()

    def _set_client_pointer(self):
        for wid in self.atom.WID:
            logger.info('_set_client_pointer: %s', self)
            self.manager.conn.xinput.XISetClientPointerChecked(
                wid, self.controller.keym[1],
                ).check()

    def _set_barrier(self):
        logger.info('_set_barrier: %s', self)
        root = self.manager.root
        x1, y1 = self.atom.POS
        x2, y2 = map(sum, zip(self.atom.POS, self.atom.DIM))
        rw, rh = root.width_in_pixels, root.height_in_pixels
        mask = (
            xfixes.BarrierDirections.PositiveX,
            xfixes.BarrierDirections.PositiveY,
            xfixes.BarrierDirections.NegativeX,
            xfixes.BarrierDirections.NegativeY,
            )
        for border, x, y, xx, yy, dirs in (
            ('top',      0, y1, rw, y1, mask[1]),
            ('right',   x2,  0, x2, rh, mask[2]),
            ('bottom',   0, y2, rw, y2, mask[3]),
            ('left',    x1,  0, x1, rh, mask[0]),
            ):
            atom = 'BARRIER_' + border.upper()
            if atom in self.atom:
                self.manager.conn.xfixes.DeletePointerBarrier(
                    self.atom.pop(atom),
                    )
            bid = self.atom[atom] = xid(self.conn.generate_id())
            self.manager.conn.xfixes.CreatePointerBarrierChecked(
                bid, self.window,
                x, y, xx, yy, dirs,
                1, [self.controller.mptr.deviceid],
                ).check()

    def _set_pointer(self):
        logger.info('_set_pointer: %s', self)
        w = self.manager.root.width_in_pixels/2 #FIXME
        h = self.manager.root.height_in_pixels/2 #FIXME
        self.manager.conn.xinput.XIWarpPointerChecked(
            0, self.window, 0, 0, 0, 0,
            FP1616(w/2), FP1616(h/2),
            self.controller.keym[1],
            ).check()

    def _set_focus(self):
        logger.info('_set_focus: %s', self)
        focus_event = struct.pack('BB2xIB23x', 9, 0, self.atom.WID[0], 0)
        self.manager.conn.core.SendEventChecked(
            0,
            self.atom.WID[0],
            xproto.EventMask.FocusChange,
            focus_event,
            ).check()


#FIXME: workarounds to incomplete library generation ------------------#

import xcb.xcb
_xcb = xcb.xcb

#...avoid deprecated 2.x relative import semantics
xcb.__dict__.update(_xcb.__dict__)
sys.modules['xcb.xcb'] = xcb

#...save a copy of parent for manual unpacking
class Struct(_Repr, _xcb.Struct):

    def __init__(self, parent, *args):
        _xcb.Struct.__init__(self, parent, *args)
        self.__parent__ = parent

class Reply(_Repr, _xcb.Reply):

    def __init__(self, parent, *args):
        _xcb.Reply.__init__(self, parent, *args)
        self.__parent__ = parent
        self.response_type = struct.unpack_from('=B', parent)[0]

class Event(_Repr, _xcb.Event):

    __xge__ = mapo.automap()
    __xge__[131][1]['xx2x4x2xHIHHB11x'].update(enumerate((
        'deviceid',
        'time',
        'num_classes',
        'sourceid',
        'reason',
        )))
    __xge__[131][11]['xx2x4x2xHIIH10x'].update(enumerate((
        'deviceid',
        'time',
        'flags',
        'num_infos',
        )))
    __xge__[131][13]['xx2x4x2xHIIHHI4x'].update(enumerate((
        'deviceid',
        'time',
        'detail',
        'sourceid',
        'valuators_len',
        'flags',
        )))
    __xge__[131][14] = __xge__[131][13]
    __xge__[131][15] = __xge__[131][13]
    __xge__[131][16] = __xge__[131][13]

    def __init__(self, parent, *args):
        _xcb.Event.__init__(self, parent, *args)
        cls = self.__class__
        ns = mapo.record()
        ns.response_type, ns.extension = (
            struct.unpack_from('=BB', parent)
            )

        if ns.response_type == 35 and ns.extension in cls.__xge__:
            (ns.xgevent,) = struct.unpack_from('=8xH', parent)
            fmt = str()
            attrs = list()
            info = cls.__xge__[ns.extension][ns.xgevent]
            if info:
                (fmt, attrs), = info.viewitems()
                ns.update(zip(
                    (attrs[i] for i in range(len(attrs))),
                    struct.unpack_from(fmt, parent),
                    ))

        for key, attr in ns.iteritems():
            setattr(self, key, attr)

        return self

#...BEFORE core/extension import!
xcb.Struct = Struct
xcb.Reply = Reply
xcb.Event = Event

from xcb import (
    randr,
    render,
    xinput,
    xfixes,
    xproto,
    )

def XISelectEvents(self, window, masks):
    buf = cStringIO.StringIO()
    buf.write(struct.pack('=xx2xIH2x', window, len(masks)))
    for deviceid, mask in masks:
        buf.write(struct.pack('=HHI', deviceid, 1, mask))
    return self.send_request(
        xcb.Request(buf.getvalue(), 46, True, False),
        xcb.VoidCookie(),
        )

def _XIChangeProperty(self, chk, devid, mode, form, prop, typ, items):
    fmt = {8: 'B', 16: 'H', 32: 'I'}
    buf = cStringIO.StringIO()
    buf.write(struct.pack(
        '=xx2xHBBIII', devid, mode, form, prop, typ, len(items),
        ))
    buf.write(struct.pack(
        '={0}{1}'.format(len(items), fmt[form]),
        *items
        ))
    return self.send_request(
        xcb.Request(buf.getvalue(), 57, True, chk),
        xcb.VoidCookie(),
        )

def XIChangeProperty(self, *ch):
    return _XIChangeProperty(self, False, *ch)

def XIChangePropertyChecked(self, *ch):
    return _XIChangeProperty(self, True, *ch)

def _XIChangeHierarchy(self, chk, changes):
    fmt = {
        1: _XIChangeHierarchy_AddMaster,
        2: '',
        3: '=HHHH',
        4: '=HHH2x',
        }
    buf = cStringIO.StringIO()
    buf.write(struct.pack('=xx2xB3x', len(changes)))
    for ch in changes:
        ch, chtyp = ch[1:], ch[0]
        chfmt = fmt[chtyp]
        if hasattr(chfmt, '__call__'):
            ch, chfmt = chfmt(*ch)
        chsz = struct.calcsize(chfmt)
        chbuf = struct.pack(chfmt, chtyp, chsz/4, *ch)
        buf.write(chbuf)
    return self.send_request(
        xcb.Request(buf.getvalue(), 43, True, chk),
        xcb.VoidCookie(),
        )

def _XIChangeHierarchy_AddMaster(send_core, enable, name):
    nl = len(name)
    return (nl, send_core, enable, name), '=HHHBB{}s'.format(nl + (nl % 4))

def XIChangeHierarchy(self, *ch):
    return _XIChangeHierarchy(self, False, *ch)

def XIChangeHierarchyChecked(self, *ch):
    return _XIChangeHierarchy(self, True, *ch)

xinput.xinputExtension.XISelectEvents = XISelectEvents
xinput.xinputExtension.XIChangeProperty = XIChangeProperty
xinput.xinputExtension.XIChangePropertyChecked = XIChangePropertyChecked
xinput.xinputExtension.XIChangeHierarchy = XIChangeHierarchy
xinput.xinputExtension.XIChangeHierarchyChecked = XIChangeHierarchyChecked

#----------------------------------------------------------------------#


if __name__ == '__main__':
    __package__ = 'xconsole'
    manager = Manager(*sys.argv[1:])
    manager.main_loop()
