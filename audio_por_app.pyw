# -*- coding: utf-8 -*-
"""Audio por app: elige por qué salida suena cada programa.

Ejemplo: Valorant por los auriculares y la música de Chrome por el parlante
Bluetooth, al mismo tiempo.

Solo biblioteca estándar (ctypes + tkinter). Habla directo con las APIs de
audio de Windows, las mismas que usa Configuración > Sonido > Mezclador de
volumen, así que la elección la guarda Windows: se mantiene aunque cierres
este programa o reinicies el PC.
"""

import base64
import colorsys
import ctypes
import math
import os
import struct
import uuid
import winreg
import zlib
import tkinter as tk
from ctypes import POINTER, byref, c_int, c_long, c_uint, c_ulong, c_ushort, c_void_p, c_wchar_p, wintypes
from tkinter import font as tkfont, messagebox, ttk


# ── Tipos y constantes de Windows ────────────────────────────────────────────

class GUID(ctypes.Structure):
    _fields_ = [("bytes", ctypes.c_ubyte * 16)]


def guid(texto):
    g = GUID()
    ctypes.memmove(byref(g), uuid.UUID(texto).bytes_le, 16)
    return g


class PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", GUID), ("pid", c_ulong)]


class PROPVARIANT(ctypes.Structure):
    _fields_ = [("vt", c_ushort), ("r1", c_ushort), ("r2", c_ushort), ("r3", c_ushort),
                ("valor", c_void_p), ("extra", c_void_p)]


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", c_long),
                ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]


CLSID_MMDeviceEnumerator = guid("bcde0395-e52f-467c-8e3d-c4579291692e")
IID_IMMDeviceEnumerator = guid("a95664d2-9614-4f35-a746-de8db63617e6")
IID_IAudioSessionManager2 = guid("77aa99a0-1bd6-484f-8bc7-2c654c9a9b6f")
IID_IAudioSessionControl2 = guid("bfb7ff88-7239-4fc9-8fa2-07c950be9c6d")
PKEY_Device_FriendlyName = PROPERTYKEY(guid("a45c254e-df1c-4efd-8020-67d146a850e0"), 14)

E_RENDER = 0                     # salida (no micrófono)
E_CONSOLE, E_MULTIMEDIA = 0, 1   # los dos "roles" que usan los programas normales
DEVICE_STATE_ACTIVE = 1
CLSCTX_ALL = 23
STGM_READ = 0
VT_LPWSTR = 31
SESION_ACTIVA, SESION_EXPIRADA = 1, 2
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TH32CS_SNAPPROCESS = 0x2

# Windows guarda la salida de cada programa con la ruta completa del dispositivo.
PREFIJO_DISPOSITIVO = "\\\\?\\SWD#MMDEVAPI#"
SUFIJO_DISPOSITIVO = "#{e6327cad-dcec-4949-ae8a-991e976a79d2}"


# ── DLLs ─────────────────────────────────────────────────────────────────────

ole32 = ctypes.WinDLL("ole32")
combase = ctypes.WinDLL("combase")
kernel32 = ctypes.WinDLL("kernel32")
version = ctypes.WinDLL("version")


def _firma(fn, restype, *argtypes):
    fn.restype, fn.argtypes = restype, argtypes


_firma(ole32.CoInitializeEx, c_long, c_void_p, c_ulong)
_firma(ole32.CoCreateInstance, c_long, POINTER(GUID), c_void_p, c_ulong, POINTER(GUID), POINTER(c_void_p))
_firma(ole32.CoTaskMemFree, None, c_void_p)
_firma(ole32.PropVariantClear, c_long, POINTER(PROPVARIANT))
_firma(combase.WindowsCreateString, c_long, c_wchar_p, c_uint, POINTER(c_void_p))
_firma(combase.WindowsDeleteString, c_long, c_void_p)
_firma(combase.WindowsGetStringRawBuffer, c_wchar_p, c_void_p, POINTER(c_uint))
_firma(combase.RoGetActivationFactory, c_long, c_void_p, POINTER(GUID), POINTER(c_void_p))
_firma(kernel32.OpenProcess, wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
_firma(kernel32.QueryFullProcessImageNameW, wintypes.BOOL,
       wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, POINTER(wintypes.DWORD))
_firma(kernel32.CloseHandle, wintypes.BOOL, wintypes.HANDLE)
_firma(kernel32.CreateToolhelp32Snapshot, wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD)
_firma(kernel32.Process32FirstW, wintypes.BOOL, wintypes.HANDLE, POINTER(PROCESSENTRY32W))
_firma(kernel32.Process32NextW, wintypes.BOOL, wintypes.HANDLE, POINTER(PROCESSENTRY32W))
_firma(version.GetFileVersionInfoSizeW, wintypes.DWORD, wintypes.LPCWSTR, POINTER(wintypes.DWORD))
_firma(version.GetFileVersionInfoW, wintypes.BOOL, wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, c_void_p)
_firma(version.VerQueryValueW, wintypes.BOOL, c_void_p, wintypes.LPCWSTR, POINTER(c_void_p), POINTER(c_uint))

# COM tiene que estar iniciado en este hilo antes de cualquier llamada de audio.
ole32.CoInitializeEx(None, 2)  # 2 = apartamento de un hilo, el mismo que usa Tk


def revisar(hr):
    """Los métodos COM devuelven un código: negativo es error."""
    if hr < 0:
        raise ctypes.WinError(hr)
    return hr


class Com:
    """Un objeto COM. Sus métodos se llaman por su posición en la tabla (vtable)."""

    def __init__(self, ptr):
        self.ptr = ptr

    def call(self, indice, argtypes, *args, restype=c_long):
        tabla = ctypes.cast(self.ptr, POINTER(POINTER(c_void_p)))[0]
        metodo = ctypes.WINFUNCTYPE(restype, c_void_p, *argtypes)(tabla[indice])
        resultado = metodo(self.ptr, *args)
        return revisar(resultado) if restype is c_long else resultado

    def obj(self, indice, argtypes=(), *args):
        """Método cuyo último parámetro devuelve otro objeto COM."""
        out = c_void_p()
        self.call(indice, (*argtypes, POINTER(c_void_p)), *args, byref(out))
        return Com(out)

    def num(self, indice, tipo=c_uint):
        """Método que devuelve un solo número."""
        out = tipo()
        self.call(indice, (POINTER(tipo),), byref(out))
        return out.value

    def query(self, iid):
        return self.obj(0, (POINTER(GUID),), byref(iid))

    def __del__(self):
        try:
            if self.ptr.value:
                self.call(2, (), restype=c_ulong)  # Release
                self.ptr = c_void_p()
        except Exception:
            pass


# ── Salidas de audio y programas que están sonando ───────────────────────────

def _enumerador():
    out = c_void_p()
    revisar(ole32.CoCreateInstance(byref(CLSID_MMDeviceEnumerator), None, CLSCTX_ALL,
                                   byref(IID_IMMDeviceEnumerator), byref(out)))
    return Com(out)


def _id_dispositivo(dev):
    p = c_void_p()
    dev.call(5, (POINTER(c_void_p),), byref(p))
    try:
        return ctypes.wstring_at(p.value)
    finally:
        ole32.CoTaskMemFree(p)


def _nombre_dispositivo(dev):
    props = dev.obj(4, (c_ulong,), STGM_READ)
    pv = PROPVARIANT()
    props.call(5, (POINTER(PROPERTYKEY), POINTER(PROPVARIANT)), byref(PKEY_Device_FriendlyName), byref(pv))
    try:
        return ctypes.wstring_at(pv.valor) if pv.vt == VT_LPWSTR and pv.valor else "Salida sin nombre"
    finally:
        ole32.PropVariantClear(byref(pv))


def escanear():
    """Lee el estado del audio en este momento.

    Devuelve (salidas, id_predeterminada, sesiones):
      salidas   [(id, nombre)] de las salidas conectadas
      sesiones  {pid: está_sonando} de los programas con audio abierto
    """
    en = _enumerador()
    salidas, sesiones = [], {}
    coleccion = en.obj(3, (c_int, c_ulong), E_RENDER, DEVICE_STATE_ACTIVE)
    for i in range(coleccion.num(3)):
        dev = coleccion.obj(4, (c_uint,), i)
        salidas.append((_id_dispositivo(dev), _nombre_dispositivo(dev)))
        # Cada salida lleva la lista de programas que suenan por ella.
        gestor = dev.obj(3, (POINTER(GUID), c_ulong, c_void_p), byref(IID_IAudioSessionManager2), CLSCTX_ALL, None)
        lista = gestor.obj(5)
        for j in range(lista.num(3, c_int)):
            control = lista.obj(4, (c_int,), j)
            estado = control.num(3, c_int)
            if estado == SESION_EXPIRADA:
                continue
            control2 = control.query(IID_IAudioSessionControl2)
            if control2.call(15, ()) == 0:  # "Sonidos del sistema": no es un programa
                continue
            pid = control2.num(14, c_ulong)
            if pid:
                sesiones[pid] = sesiones.get(pid, False) or estado == SESION_ACTIVA
    try:
        predeterminada = _id_dispositivo(en.obj(4, (c_int, c_int), E_RENDER, E_MULTIMEDIA))
    except OSError:  # no hay ninguna salida conectada
        predeterminada = None
    return salidas, predeterminada, sesiones


def _ruta_proceso(pid):
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        buf = ctypes.create_unicode_buffer(1024)
        n = wintypes.DWORD(len(buf))
        return buf.value if kernel32.QueryFullProcessImageNameW(h, 0, buf, byref(n)) else None
    finally:
        kernel32.CloseHandle(h)


def _exe_por_lista(pid):
    """Nombre del .exe sin abrir el proceso (para los que no dejan abrirse)."""
    foto = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not foto or foto == c_void_p(-1).value:
        return None
    try:
        e = PROCESSENTRY32W()
        e.dwSize = ctypes.sizeof(e)
        ok = kernel32.Process32FirstW(foto, byref(e))
        while ok:
            if e.th32ProcessID == pid:
                return e.szExeFile
            ok = kernel32.Process32NextW(foto, byref(e))
    finally:
        kernel32.CloseHandle(foto)


def _descripcion(ruta):
    """El nombre bonito que trae el .exe: chrome.exe dice "Google Chrome"."""
    tam = version.GetFileVersionInfoSizeW(ruta, None)
    if not tam:
        return None
    buf = ctypes.create_string_buffer(tam)
    if not version.GetFileVersionInfoW(ruta, 0, tam, buf):
        return None
    p, n = c_void_p(), c_uint()
    idiomas = []
    if version.VerQueryValueW(buf, "\\VarFileInfo\\Translation", byref(p), byref(n)) and n.value >= 4:
        pares = ctypes.cast(p, POINTER(c_ushort))
        idiomas = [(pares[i], pares[i + 1]) for i in range(0, n.value // 2, 2)]
    for idioma, pagina in idiomas + [(0x0409, 0x04B0), (0x0409, 0x04E4)]:
        clave = f"\\StringFileInfo\\{idioma:04x}{pagina:04x}\\FileDescription"
        if version.VerQueryValueW(buf, clave, byref(p), byref(n)) and n.value > 1:
            texto = ctypes.wstring_at(p.value).strip()
            if texto:
                return texto
    return None


_nombres = {}


def programa(pid):
    """(clave, nombre visible). La clave agrupa los procesos del mismo .exe."""
    ruta = _ruta_proceso(pid)
    exe = os.path.basename(ruta) if ruta else _exe_por_lista(pid)
    if not exe:
        return f"pid-{pid}", f"Proceso {pid}"
    clave = exe.lower()
    if clave not in _nombres:
        _nombres[clave] = (ruta and _descripcion(ruta)) or os.path.splitext(exe)[0]
    return clave, _nombres[clave]


# ── La salida elegida para cada programa ─────────────────────────────────────

def _hstring(texto):
    h = c_void_p()
    revisar(combase.WindowsCreateString(texto, len(texto), byref(h)))
    return h


class PoliticaAudio:
    """La pieza interna de Windows que recuerda por qué salida suena cada programa.

    No está documentada por Microsoft: es la que usan la Configuración de
    Windows y EarTrumpet. El orden de los métodos es el de Windows 10 21H2+ y 11.
    """

    _ASIGNAR, _LEER, _LIMPIAR = 25, 26, 27
    _IIDS = ("ab3d4648-e242-459f-b02f-541c70306324",  # Windows 10 21H2 en adelante y 11
             "2a59116d-6c4f-45e0-a74f-707e3fef9258")  # Windows 10 anteriores

    def __init__(self):
        clase = _hstring("Windows.Media.Internal.AudioPolicyConfig")
        try:
            for iid in self._IIDS:
                out = c_void_p()
                if combase.RoGetActivationFactory(clase, byref(guid(iid)), byref(out)) >= 0:
                    self.com = Com(out)
                    break
            else:
                raise OSError("Esta versión de Windows no permite elegir la salida por programa.")
        finally:
            combase.WindowsDeleteString(clase)

    def asignar(self, pid, id_salida):
        """id_salida=None devuelve el programa a la salida predeterminada de Windows."""
        h = _hstring(PREFIJO_DISPOSITIVO + id_salida + SUFIJO_DISPOSITIVO) if id_salida else None
        try:
            for rol in (E_MULTIMEDIA, E_CONSOLE):
                self.com.call(self._ASIGNAR, (c_uint, c_int, c_int, c_void_p), pid, E_RENDER, rol, h)
        finally:
            if h:
                combase.WindowsDeleteString(h)

    def asignada(self, pid):
        """Id de la salida elegida para el programa, o None si sigue a Windows."""
        h = c_void_p()
        self.com.call(self._LEER, (c_uint, c_int, c_int, POINTER(c_void_p)), pid, E_RENDER, E_MULTIMEDIA, byref(h))
        if not h.value:
            return None
        try:
            texto = combase.WindowsGetStringRawBuffer(h, None) or ""
        finally:
            combase.WindowsDeleteString(h)
        if texto.upper().startswith(PREFIJO_DISPOSITIVO.upper()):
            texto = texto[len(PREFIJO_DISPOSITIVO):]
        return texto.rsplit("#", 1)[0].lower() or None

    def limpiar_todo(self):
        self.com.call(self._LIMPIAR, ())


# ── Aspecto ──────────────────────────────────────────────────────────────────
#
# El aspecto de Windows 11 hecho sólo con tkinter/ttk: sigue el tema claro u
# oscuro del sistema y su color de acento. Las esquinas redondeadas son imágenes
# PNG que se dibujan aquí mismo al abrir (no hay archivos aparte).
# Para ver el otro tema sin cambiar Windows: variable de entorno
# AUDIO_POR_APP_TEMA=oscuro (o =claro).

PALETAS = {
    "oscuro": {"fondo": "#202020", "tarjeta": "#2B2B2B", "tarjeta_hover": "#323232", "borde": "#1D1D1D",
               "divisor": "#232323", "control": "#373737", "control_hover": "#3D3D3D",
               "control_pulsado": "#303030", "borde_control": "#454545", "borde_control_abajo": "#3A3A3A",
               "lista": "#2C2C2C", "lista_sel": "#404040", "barra": "#6F6F6F", "barra_hover": "#9A9A9A",
               "texto": "#FFFFFF", "texto2": "#D0D0D0", "tenue": "#9E9E9E"},
    "claro": {"fondo": "#F3F3F3", "tarjeta": "#FBFBFB", "tarjeta_hover": "#F5F5F5", "borde": "#E5E5E5",
              "divisor": "#EAEAEA", "control": "#FFFFFF", "control_hover": "#F8F8F8",
              "control_pulsado": "#F3F3F3", "borde_control": "#E3E3E3", "borde_control_abajo": "#C8C8C8",
              "lista": "#F9F9F9", "lista_sel": "#E6E6E6", "barra": "#9A9A9A", "barra_hover": "#707070",
              "texto": "#1B1B1B", "texto2": "#4F4F4F", "tenue": "#666666"},
}

# Íconos de la fuente del sistema: Segoe Fluent Icons (Windows 11) o Segoe MDL2 Assets (10).
ICONO_AUDIFONOS, ICONO_BLUETOOTH, ICONO_PANTALLA, ICONO_PARLANTE, ICONO_INFO = (
    "", "", "", "", "")


def icono_salida(nombre):
    n = nombre.lower()
    if any(p in n for p in ("audífono", "audifono", "auricular", "headset", "headphone")):
        return ICONO_AUDIFONOS
    if "bluetooth" in n:
        return ICONO_BLUETOOTH
    if any(p in n for p in ("hdmi", "nvidia", "monitor", "pantalla")):
        return ICONO_PANTALLA
    return ICONO_PARLANTE


def _registro(ruta, valor):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, ruta) as k:
            return winreg.QueryValueEx(k, valor)[0]
    except OSError:
        return None


def tema_oscuro():
    forzado = os.environ.get("AUDIO_POR_APP_TEMA", "").lower()
    if forzado in ("oscuro", "claro"):
        return forzado == "oscuro"
    return _registro(r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize", "AppsUseLightTheme") == 0


def _rgb(color):
    return tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))


def _hex(rgb):
    return "#%02X%02X%02X" % tuple(rgb)


def _mezcla(a, b, t):
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def _contraste(a, b):
    def luz(rgb):
        c = [v / 255 for v in rgb]
        c = [v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4 for v in c]
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
    alto, bajo = sorted((luz(a), luz(b)), reverse=True)
    return (alto + 0.05) / (bajo + 0.05)


def color_acento(oscuro, fondo):
    """El acento de Windows ajustado al tema, y el color de texto que se lee encima."""
    abgr = _registro(r"Software\Microsoft\Windows\DWM", "AccentColor")
    base = (abgr & 0xFF, abgr >> 8 & 0xFF, abgr >> 16 & 0xFF) if isinstance(abgr, int) else (0, 120, 212)
    # Como hace Windows: en oscuro el acento se aclara y en claro se oscurece un poco.
    hacia, t = ((255, 255, 255), 0.3) if oscuro else ((0, 0, 0), 0.1)
    acento = _mezcla(base, hacia, t)
    while _contraste(acento, _rgb(fondo)) < 3 and t < 1:
        t = min(t + 0.1, 1)
        acento = _mezcla(base, hacia, t)
    negro, blanco = (0, 0, 0), (255, 255, 255)
    encima = negro if _contraste(acento, negro) >= _contraste(acento, blanco) else blanco
    return _hex(acento), _hex(encima)


def _png(ancho, alto, pixel):
    """Imagen con transparencia armada a mano: pixel(x, y) -> (r, g, b, alfa)."""
    crudo = bytearray()
    for y in range(alto):
        crudo.append(0)
        for x in range(ancho):
            crudo.extend(pixel(x + 0.5, y + 0.5))

    def trozo(tipo, datos):
        return struct.pack(">I", len(datos)) + tipo + datos + struct.pack(">I", zlib.crc32(tipo + datos))
    png = (b"\x89PNG\r\n\x1a\n" + trozo(b"IHDR", struct.pack(">IIBBBBB", ancho, alto, 8, 6, 0, 0, 0))
           + trozo(b"IDAT", zlib.compress(bytes(crudo))) + trozo(b"IEND", b""))
    return tk.PhotoImage(data=base64.b64encode(png).decode("ascii"), format="png")


def _cubre(distancia):
    """Cuánto del píxel cae dentro de la figura (0 a 1): así el borde no sale dentado."""
    return min(max(0.5 - distancia, 0.0), 1.0)


def _dist_rect(x, y, ancho, alto, radio):
    qx = abs(x - ancho / 2) - (ancho / 2 - radio)
    qy = abs(y - alto / 2) - (alto / 2 - radio)
    return math.hypot(max(qx, 0), max(qy, 0)) + min(max(qx, qy), 0) - radio


def _dist_segmento(x, y, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    t = max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(x - ax - t * dx, y - ay - t * dy)


CENTRO = 64  # px del centro de cada imagen estirable: más grande, menos trozos que repetir


def rect_redondeado(ancho, alto, radio, relleno, borde, detras, borde_abajo=None):
    """Rectángulo con esquinas redondeadas y borde de 1 px (el de abajo puede ir más marcado).

    Las esquinas se mezclan con el color que queda detrás y la imagen sale opaca:
    ttk no siempre repinta bien lo transparente y, además, lo opaco es mucho más
    rápido. Tk estira estas imágenes repitiendo su centro como baldosas: por eso
    el centro es de CENTRO px y no de 2.
    """
    f, b, ba, d = _rgb(relleno), _rgb(borde), _rgb(borde_abajo or borde), _rgb(detras)
    lleno = (*f, 255)

    def pixel(x, y):
        if min(x, ancho - x, y, alto - y) > radio + 1.5:
            return lleno
        alfa = _cubre(_dist_rect(x, y, ancho, alto, radio))
        dentro = _cubre(_dist_rect(x - 1, y - 1, ancho - 2, alto - 2, max(radio - 1, 0)))
        linea = ba if y > alto - radio - 1 else b
        return (*_mezcla(d, _mezcla(linea, f, dentro), alfa), 255)
    return _png(ancho, alto, pixel)


def circulo(diametro, color, ancho=None):
    c, r = _rgb(color), diametro / 2
    return _png(ancho or diametro, diametro,
                lambda x, y: (*c, round(_cubre(math.hypot(x - r, y - r) - r) * 255)))


def flecha(ancho, alto, color, grosor, margen):
    """La flecha hacia abajo de los menús, con margen transparente a la derecha."""
    c, g = _rgb(color), grosor / 2 + 0.5

    def pixel(x, y):
        d = min(_dist_segmento(x, y, g, g, ancho / 2, alto - g),
                _dist_segmento(x, y, ancho / 2, alto - g, ancho - g, g))
        return (*c, round(_cubre(d - grosor / 2 + 0.5) * 255))
    return _png(ancho + margen, alto, pixel)


# ── Ventana ──────────────────────────────────────────────────────────────────

DESCONOCIDA = object()


class Fila:
    """La tarjeta de un programa: avatar con su inicial, nombre, estado y el menú de salidas."""

    def __init__(self, padre, ventana, nombre):
        self.nombre = nombre
        self.pids, self.sonando, self.asignada = [], False, DESCONOCIDA
        self.ventana, px = ventana, ventana.px
        self.resaltada = False
        self.marco = ttk.Frame(padre, style="Tarjeta.TFrame", padding=(px(16), px(12), px(12), px(12)))
        self.avatar = ventana.avatar(self.marco, nombre)
        self.avatar.grid(row=0, column=0, rowspan=2, padx=(0, px(14)))
        self.titulo = ttk.Label(self.marco, text=nombre, style="Nombre.TLabel")
        self.titulo.grid(row=0, column=1, sticky="sw")
        self.estado = ttk.Label(self.marco, style="Callado.TLabel", compound="left")
        self.estado.grid(row=1, column=1, sticky="nw")
        self.combo = ttk.Combobox(self.marco, state="readonly", width=42, style="Salida.TCombobox",
                                  font=ventana.f["cuerpo"])
        self.combo.grid(row=0, column=2, rowspan=2, sticky="e", padx=(px(16), 0))
        self.combo.bind("<<ComboboxSelected>>", lambda _e: ventana.elegir(self))
        # Tk cambia la opción con la rueda del mouse: la rueda solo desplaza la lista.
        self.combo.bind("<MouseWheel>", ventana._rueda)
        self.marco.columnconfigure(1, weight=1)

    def mostrar_estado(self, sonando):
        if sonando:
            self.estado.configure(text="Sonando", image=self.ventana.img["punto"], style="Estado.TLabel")
        else:
            self.estado.configure(text="En silencio", image="", style="Callado.TLabel")

    def resaltar(self, activa):
        """Fondo un poco más claro mientras el mouse está encima, como en Configuración."""
        if activa == self.resaltada:
            return
        self.resaltada = activa
        for w in (self.marco, self.titulo, self.estado):
            w.state(["hover"] if activa else ["!hover"])
        self.avatar.configure(bg=self.ventana.p["tarjeta_hover" if activa else "tarjeta"])


class Ventana:
    INTERVALO_MS = 1500  # cada cuánto se revisa si cambió algo

    def __init__(self, raiz):
        self.raiz = raiz
        self.politica = PoliticaAudio()
        self.salidas, self.predeterminada = None, None
        self.filas = {}
        self._estilo()
        px = self.px

        cuerpo = ttk.Frame(raiz, padding=(px(28), px(20), px(28), px(20)))
        cuerpo.pack(fill="both", expand=True)
        cuerpo.columnconfigure(0, weight=1)
        cuerpo.rowconfigure(2, weight=1)
        ttk.Label(cuerpo, text="Audio por app", style="Titulo.TLabel").grid(row=0, sticky="w")
        ttk.Label(cuerpo, text="Elige por qué salida suena cada programa.",
                  style="Tenue.TLabel").grid(row=1, sticky="w", pady=(0, px(20)))

        # Salidas y programas se desplazan juntos si no caben, como una página de Configuración.
        marco = ttk.Frame(cuerpo)
        marco.grid(row=2, sticky="nsew")
        marco.columnconfigure(0, weight=1)
        marco.rowconfigure(0, weight=1)
        self.lienzo = tk.Canvas(marco, bg=self.p["fondo"], highlightthickness=0, bd=0)
        self.lienzo.grid(row=0, column=0, sticky="nsew")
        self.barra = ttk.Scrollbar(marco, orient="vertical", command=self.lienzo.yview,
                                   style="Fina.Vertical.TScrollbar")
        self.lienzo.configure(yscrollcommand=self.barra.set)
        self.lista = ttk.Frame(self.lienzo)
        self.lista.columnconfigure(0, weight=1)
        hueco = self.lienzo.create_window(0, 0, window=self.lista, anchor="nw")
        self.lienzo.bind("<Configure>", lambda e: self.lienzo.itemconfigure(hueco, width=e.width))
        self.lista.bind("<Configure>", lambda e: self._ajustar_lista())
        raiz.bind_all("<MouseWheel>", self._rueda)
        raiz.bind_all("<Enter>", self._resaltar, add="+")
        raiz.bind_all("<Leave>", self._resaltar, add="+")

        self.titulo_salidas = ttk.Label(self.lista, style="Seccion.TLabel")
        self.titulo_salidas.grid(sticky="w", pady=(0, px(8)))
        # Esquinas de 6 px: el contenido entra 3 px para no tapar la curva.
        self.grupo = ttk.Frame(self.lista, style="Grupo.TFrame", padding=px(3))
        self.grupo.grid(sticky="ew")
        self.grupo.columnconfigure(0, weight=1)
        ttk.Label(self.lista, text="Programas con audio", style="Seccion.TLabel").grid(
            sticky="w", pady=(px(24), px(8)))
        self.programas = ttk.Frame(self.lista)
        self.programas.grid(sticky="ew")
        self.programas.columnconfigure(0, weight=1)
        self.vacio = ttk.Label(self.lista, style="Tenue.TLabel",
                               text="Ningún programa tiene el audio abierto. Pon algo a sonar y aparece aquí.")
        self.vacio.grid(sticky="w")

        pie = ttk.Frame(cuerpo)
        pie.grid(row=3, sticky="ew", pady=(px(18), 0))
        pie.columnconfigure(1, weight=1)
        if self.iconos:
            ttk.Label(pie, text=ICONO_INFO, style="Info.TLabel").grid(row=0, column=0, padx=(0, px(10)))
        self.aviso = ttk.Label(pie, style="Tenue.TLabel", wraplength=px(380),
                               text="Si un juego no cambia de salida, reinícialo: algunos la leen solo al abrir.")
        self.aviso.grid(row=0, column=1, sticky="w")
        boton = ttk.Button(pie, text="Todo a la de Windows", style="Secundario.TButton", command=self.restablecer)
        boton.grid(row=0, column=2, padx=(px(16), 0))
        # El aviso usa el ancho que deja el botón y baja de línea si no cabe.
        pie.bind("<Configure>", lambda e: self.aviso.configure(
            wraplength=max(px(160), e.width - boton.winfo_reqwidth() - px(60))))

        self.refrescar()

    def px(self, n):
        """Píxeles a la escala de la pantalla (100 %, 150 %...)."""
        return max(1, round(n * self.escala)) if n else 0

    def _estilo(self):
        r = self.raiz
        self.escala = r.winfo_fpixels("1i") / 96
        self.oscuro = tema_oscuro()
        p = self.p = dict(PALETAS["oscuro" if self.oscuro else "claro"])
        p["acento"], p["sobre_acento"] = color_acento(self.oscuro, p["tarjeta"])
        px = self.px

        familias = set(tkfont.families(r))

        def fuente(*nombres):
            return next((n for n in nombres if n in familias), "Segoe UI")
        # Windows lista las variantes con el nombre cortado a 31 letras ("...Display Semib").
        display = fuente("Segoe UI Variable Display Semib", "Segoe UI Semibold")
        texto = fuente("Segoe UI Variable Text", "Segoe UI")
        semi = fuente("Segoe UI Variable Text Semibold", "Segoe UI Semibold")
        chica = fuente("Segoe UI Variable Small", "Segoe UI")
        chica_semi = fuente("Segoe UI Variable Small Semibol", "Segoe UI Semibold")
        self.iconos = next((n for n in ("Segoe Fluent Icons", "Segoe MDL2 Assets") if n in familias), None)
        # Tamaños en píxeles (negativos para Tk), los mismos de la Configuración de Windows 11.
        self.f = f = {"titulo": (display, -px(28)), "cuerpo": (texto, -px(14)), "fuerte": (semi, -px(14)),
                      "chica": (chica, -px(12)), "pastilla": (chica_semi, -px(11)),
                      "icono": (self.iconos or texto, -px(16)), "info": (self.iconos or texto, -px(14))}

        r.configure(bg=p["fondo"])
        s = ttk.Style(r)
        s.theme_use("clam")
        s.configure(".", background=p["fondo"], foreground=p["texto"], font=f["cuerpo"])
        s.configure("Titulo.TLabel", font=f["titulo"])
        s.configure("Tenue.TLabel", foreground=p["tenue"])
        s.configure("Info.TLabel", foreground=p["tenue"], font=f["info"])
        s.configure("Seccion.TLabel", font=f["fuerte"])

        img = self.img = {}
        rt, rc = px(6), px(4)  # radio de las tarjetas y de los controles

        def imagen(nombre, *estados, **opciones):
            """Elemento de ttk que estira la imagen sin deformar las esquinas."""
            opciones.setdefault("width", 1)   # que mande el contenido, no el tamaño de la imagen
            opciones.setdefault("height", 1)
            s.element_create(nombre, "image", img[estados[0]],
                             *((e, img[i]) for e, i in zip(estados[1::2], estados[2::2])),
                             sticky="nsew", **opciones)

        # Tarjetas
        lado = 2 * rt + 2 + CENTRO
        img["tarjeta"] = rect_redondeado(lado, lado, rt, p["tarjeta"], p["borde"], p["fondo"])
        img["tarjeta_hover"] = rect_redondeado(lado, lado, rt, p["tarjeta_hover"], p["borde"], p["fondo"])
        imagen("Tarjeta.fondo", "tarjeta", "hover", "tarjeta_hover", border=rt + 1, padding=0)
        imagen("Grupo.fondo", "tarjeta", border=rt + 1, padding=0)
        s.layout("Tarjeta.TFrame", [("Tarjeta.fondo", {"sticky": "nsew"})])
        s.layout("Grupo.TFrame", [("Grupo.fondo", {"sticky": "nsew"})])
        s.configure("Plano.TFrame", background=p["tarjeta"])
        s.configure("Divisor.TFrame", background=p["divisor"])
        for estilo, letra, color in (("Nombre", "cuerpo", "texto"), ("Estado", "chica", "texto2"),
                                     ("Callado", "chica", "tenue"), ("Salida", "cuerpo", "texto"),
                                     ("IconoSalida", "icono", "texto2")):
            s.configure(f"{estilo}.TLabel", background=p["tarjeta"], foreground=p[color], font=f[letra])
            s.map(f"{estilo}.TLabel", background=[("hover", p["tarjeta_hover"])])
        img["punto"] = circulo(px(8), p["acento"], ancho=px(8) + px(7))

        # Pastilla "Predeterminada"
        alto = tkfont.Font(r, font=f["pastilla"]).metrics("linespace") + px(4)
        alto += 1 - alto % 2
        img["pastilla"] = rect_redondeado(alto + CENTRO, alto, alto // 2, p["acento"], p["acento"], p["tarjeta"])
        imagen("Pastilla.fondo", "pastilla", border=alto // 2, padding=(px(9), px(2), px(9), px(2)))
        s.layout("Pastilla.TLabel", [("Pastilla.fondo", {"sticky": "nsew", "children": [
            ("Label.label", {"sticky": "nsew"})]})])
        s.configure("Pastilla.TLabel", background=p["tarjeta"], foreground=p["sobre_acento"], font=f["pastilla"])

        # Controles: el menú de salidas va sobre las tarjetas y el botón sobre el fondo.
        lado = 2 * rc + 2 + CENTRO
        for control, detras in (("campo", "tarjeta"), ("boton", "fondo")):
            for nombre, relleno, borde, abajo in (
                    ("", "control", "borde_control", "borde_control_abajo"),
                    ("_hover", "control_hover", "borde_control", "borde_control_abajo"),
                    ("_pulsado", "control_pulsado", "borde_control", "borde_control"),
                    ("_foco", "control", "acento", "acento")):
                img[control + nombre] = rect_redondeado(lado, lado, rc, p[relleno], p[borde], p[detras], p[abajo])
        img["flecha"] = flecha(px(10), px(6), p["texto2"], max(1.0, 1.1 * self.escala), px(10))
        imagen("Campo.fondo", "campo", "pressed", "campo_pulsado", "focus", "campo_foco", "hover", "campo_hover",
               border=rc + 1, padding=0)
        s.element_create("Campo.flecha", "image", img["flecha"], sticky="")
        s.layout("Salida.TCombobox", [("Campo.fondo", {"sticky": "nsew", "children": [
            ("Campo.flecha", {"side": "right", "sticky": ""}),
            ("Combobox.padding", {"expand": "1", "sticky": "nsew", "children": [
                ("Combobox.textarea", {"sticky": "nsew"})]})]})])
        s.configure("Salida.TCombobox", padding=(px(11), px(6), px(6), px(6)), foreground=p["texto"],
                    arrowsize=px(10))
        # El texto elegido no se pinta como selección: queda del color del campo.
        s.map("Salida.TCombobox", foreground=[("readonly", p["texto"])],
              selectforeground=[("readonly", p["texto"])],
              selectbackground=[("pressed", p["control_pulsado"]), ("hover", p["control_hover"]),
                                ("readonly", p["control"])])
        s.configure("ComboboxPopdownFrame", background=p["lista"], bordercolor=p["borde_control"],
                    lightcolor=p["lista"], darkcolor=p["lista"], relief="solid", borderwidth=1)
        r.option_add("*TCombobox*Listbox.background", p["lista"])
        r.option_add("*TCombobox*Listbox.foreground", p["texto"])
        r.option_add("*TCombobox*Listbox.selectBackground", p["lista_sel"])
        r.option_add("*TCombobox*Listbox.selectForeground", p["texto"])
        r.option_add("*TCombobox*Listbox.font", tkfont.Font(r, font=f["cuerpo"]))
        r.option_add("*TCombobox*Listbox.borderWidth", px(4))  # aire alrededor de la lista
        r.option_add("*TCombobox*Listbox.relief", "flat")
        r.option_add("*TCombobox*Listbox.highlightThickness", 0)
        r.option_add("*TCombobox*Listbox.activeStyle", "none")

        imagen("Boton.fondo", "boton", "pressed", "boton_pulsado", "focus", "boton_foco",
               "active", "boton_hover", border=rc + 1, padding=0)
        s.layout("Secundario.TButton", [("Boton.fondo", {"sticky": "nsew", "children": [
            ("Button.padding", {"sticky": "nsew", "children": [("Button.label", {"sticky": "nsew"})]})]})])
        s.configure("Secundario.TButton", padding=(px(14), px(6)), foreground=p["texto"], anchor="center")
        s.map("Secundario.TButton", foreground=[("pressed", p["texto2"])])

        # Barra de desplazamiento fina y redondeada, sin flechas
        ancho = px(6) | 1
        for nombre in ("barra", "barra_hover"):
            img[nombre] = rect_redondeado(ancho, ancho + CENTRO, ancho / 2, p[nombre], p[nombre], p["fondo"])
        imagen("Fina.Vertical.Scrollbar.thumb", "barra", "active", "barra_hover", "pressed", "barra_hover",
               border=ancho // 2, padding=0, width=ancho, height=px(24))
        s.layout("Fina.Vertical.TScrollbar", [("Vertical.Scrollbar.trough", {"sticky": "ns", "children": [
            ("Fina.Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})]})])
        s.configure("Fina.Vertical.TScrollbar", troughcolor=p["fondo"], background=p["fondo"],
                    bordercolor=p["fondo"], lightcolor=p["fondo"], darkcolor=p["fondo"])

    def avatar(self, padre, nombre):
        """Círculo con la inicial del programa; el color sale del nombre y no cambia."""
        d = self.px(32)
        lienzo = tk.Canvas(padre, width=d, height=d, bg=self.p["tarjeta"], highlightthickness=0, bd=0)
        tono = zlib.crc32(nombre.lower().encode()) % 360 / 360
        luz_fondo, luz_letra, sat = (0.34, 0.86, 0.32) if self.oscuro else (0.88, 0.30, 0.50)
        fondo = _hex(round(v * 255) for v in colorsys.hls_to_rgb(tono, luz_fondo, sat))
        letra = _hex(round(v * 255) for v in colorsys.hls_to_rgb(tono, luz_letra, sat + 0.2))
        if "avatar" + fondo not in self.img:
            self.img["avatar" + fondo] = circulo(d, fondo)
        lienzo.create_image(d / 2, d / 2, image=self.img["avatar" + fondo])
        inicial = next((c for c in nombre if c.isalnum()), "?").upper()
        lienzo.create_text(d / 2, d / 2, text=inicial, fill=letra, font=self.f["fuerte"])
        return lienzo

    def _resaltar(self, _e=None):
        x, y = self.raiz.winfo_pointerxy()
        debajo = self.raiz.winfo_containing(x, y)
        ruta = str(debajo) if debajo else ""
        for f in self.filas.values():
            marco = str(f.marco)
            f.resaltar(ruta == marco or ruta.startswith(marco + "."))

    def _pintar_salidas(self, salidas, predeterminada):
        px, n = self.px, len(salidas)
        self.titulo_salidas.configure(text=f"{n} salida{'s' if n != 1 else ''} detectada{'s' if n != 1 else ''}")
        for w in self.grupo.winfo_children():
            w.destroy()
        if not salidas:
            ttk.Label(self.grupo, text="No hay ninguna salida de audio conectada.",
                      style="Callado.TLabel").grid(sticky="w", padx=px(14), pady=px(10))
        for k, (i, nombre) in enumerate(salidas):
            if k:
                ttk.Frame(self.grupo, style="Divisor.TFrame", height=1).grid(sticky="ew")
            fila = ttk.Frame(self.grupo, style="Plano.TFrame", padding=(px(13), px(9), px(11), px(9)))
            fila.grid(sticky="ew")
            fila.columnconfigure(1, weight=1)
            if self.iconos:
                ttk.Label(fila, text=icono_salida(nombre), style="IconoSalida.TLabel").grid(
                    row=0, column=0, padx=(0, px(14)))
            ttk.Label(fila, text=nombre, style="Salida.TLabel").grid(row=0, column=1, sticky="w")
            if i == predeterminada:
                ttk.Label(fila, text="Predeterminada", style="Pastilla.TLabel").grid(
                    row=0, column=2, padx=(px(12), 0))

    # Opciones del menú: la primera siempre es "seguir a Windows".
    def _opciones(self):
        return ["Igual que Windows"] + [n for _, n in self.salidas]

    def _texto(self, id_salida):
        if id_salida is None:
            return self._opciones()[0]
        for i, nombre in self.salidas:
            if i.lower() == id_salida:
                return nombre
        return "⚠ No conectada: suena por la de Windows"

    def refrescar(self):
        try:
            self.pintar(*escanear())
        except OSError as e:
            self.aviso.configure(text=f"No se pudo leer el audio: {e}")
        self.raiz.after(self.INTERVALO_MS, self.refrescar)

    def pintar(self, salidas, predeterminada, sesiones):
        if (salidas, predeterminada) != (self.salidas, self.predeterminada):
            self.salidas, self.predeterminada = salidas, predeterminada
            self._pintar_salidas(salidas, predeterminada)
            for f in self.filas.values():
                f.combo["values"] = self._opciones()
                f.asignada = DESCONOCIDA

        programas = {}
        for pid, sonando in sesiones.items():
            clave, nombre = programa(pid)
            p = programas.setdefault(clave, [nombre, [], False])
            p[1].append(pid)
            p[2] = p[2] or sonando

        for clave in [c for c in self.filas if c not in programas]:
            self.filas.pop(clave).marco.destroy()
        for clave, (nombre, pids, sonando) in programas.items():
            f = self.filas.get(clave)
            if f is None:
                f = self.filas[clave] = Fila(self.programas, self, nombre)
                f.combo["values"] = self._opciones()
            f.pids, f.sonando = sorted(pids), sonando
            f.mostrar_estado(sonando)
            asignada = self._asignada(f.pids)
            if asignada != f.asignada:
                f.asignada = asignada
                f.combo.set(self._texto(asignada))

        orden = sorted(self.filas.values(), key=lambda f: (not f.sonando, f.nombre.lower()))
        for i, f in enumerate(orden):
            f.marco.grid(row=i, column=0, sticky="ew", pady=(0, self.px(4)))
        if self.filas:
            self.vacio.grid_remove()
        else:
            self.vacio.grid()
        self.lista.update_idletasks()  # que Tk mida las tarjetas nuevas antes de ajustar el alto
        self._ajustar_lista()

    def _ajustar_lista(self):
        alto = self.lista.winfo_reqheight()
        tope = int(self.raiz.winfo_screenheight() * 0.6)
        self.lienzo.configure(width=self.lista.winfo_reqwidth(), height=max(min(alto, tope), 1),
                              scrollregion=(0, 0, 0, alto))
        if alto > tope:
            self.barra.grid(row=0, column=1, sticky="ns", padx=(self.px(8), 0))
        else:
            self.barra.grid_remove()
            self.lienzo.yview_moveto(0)

    def _rueda(self, e):
        dentro = str(e.widget).startswith(str(self.lienzo)) and "popdown" not in str(e.widget)
        if self.barra.winfo_ismapped() and dentro:
            self.lienzo.yview_scroll(-1 if e.delta > 0 else 1, "units")
        return "break"

    def _asignada(self, pids):
        for pid in pids:
            try:
                id_salida = self.politica.asignada(pid)
            except OSError:
                continue
            if id_salida:
                return id_salida
        return None

    def elegir(self, fila):
        i = fila.combo.current()
        destino = self.salidas[i - 1] if i > 0 else None
        try:
            for pid in fila.pids:
                self.politica.asignar(pid, destino[0] if destino else None)
        except OSError as e:
            self.aviso.configure(text=f"No se pudo cambiar {fila.nombre}: {e}")
            fila.asignada = DESCONOCIDA
            return
        fila.asignada = destino[0].lower() if destino else None
        fila.combo.selection_clear()
        self.raiz.focus_set()
        self.aviso.configure(text=f"{fila.nombre}  →  {destino[1] if destino else 'la salida de Windows'}")

    def restablecer(self):
        if not messagebox.askyesno("Audio por app",
                                   "¿Devolver todos los programas a la salida predeterminada de Windows?"):
            return
        try:
            self.politica.limpiar_todo()
        except OSError as e:
            self.aviso.configure(text=f"No se pudo devolver todo a la salida de Windows: {e}")
            return
        for f in self.filas.values():
            f.asignada = DESCONOCIDA
        self.aviso.configure(text="Todos los programas vuelven a la salida de Windows.")


def ventana_nativa(raiz, oscuro, fondo):
    """Barra de título y esquinas como las de Windows 11. Lo que Windows 10 no conoce, lo ignora."""
    try:
        raiz.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(raiz.winfo_id())
        r, g, b = _rgb(fondo)
        atributos = [(33, 2),                          # esquinas redondeadas
                     (35, r | g << 8 | b << 16)]       # barra de título del color de la ventana
        if oscuro:
            atributos.insert(0, (20, 1))               # barra de título oscura
        for atributo, valor in atributos:
            v = c_int(valor)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, atributo, byref(v), ctypes.sizeof(v))
    except (AttributeError, OSError):
        pass


def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # texto nítido en pantallas con escala
    except (AttributeError, OSError):
        pass
    raiz = tk.Tk()
    raiz.title("Audio por app")
    try:
        ventana = Ventana(raiz)
    except OSError as e:
        messagebox.showerror("Audio por app", str(e))
        return
    raiz.minsize(ventana.px(600), 0)
    ventana_nativa(raiz, ventana.oscuro, ventana.p["fondo"])
    raiz.mainloop()


if __name__ == "__main__":
    main()
