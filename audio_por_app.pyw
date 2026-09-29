# -*- coding: utf-8 -*-
"""Audio por app: elige por qué salida suena cada programa.

Ejemplo: Valorant por los auriculares y la música de Chrome por el parlante
Bluetooth, al mismo tiempo.

Solo biblioteca estándar (ctypes + tkinter). Habla directo con las APIs de
audio de Windows, las mismas que usa Configuración > Sonido > Mezclador de
volumen, así que la elección la guarda Windows: se mantiene aunque cierres
este programa o reinicies el PC.
"""

import ctypes
import os
import uuid
import tkinter as tk
from ctypes import POINTER, byref, c_int, c_long, c_uint, c_ulong, c_ushort, c_void_p, c_wchar_p, wintypes
from tkinter import messagebox, ttk


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


# ── Ventana ──────────────────────────────────────────────────────────────────

FONDO, TARJETA, CAMPO, BORDE = "#15171c", "#1e2128", "#272b33", "#343a45"
TEXTO, TENUE, ACENTO = "#e9eaee", "#8a92a0", "#3ecf8e"
DESCONOCIDA = object()


class Fila:
    def __init__(self, padre, ventana, nombre):
        self.nombre = nombre
        self.pids, self.sonando, self.asignada = [], False, DESCONOCIDA
        self.marco = ttk.Frame(padre, style="Tarjeta.TFrame", padding=(14, 10))
        ttk.Label(self.marco, text=nombre, style="Nombre.TLabel").grid(row=0, column=0, sticky="w")
        self.estado = ttk.Label(self.marco, style="Callado.TLabel")
        self.estado.grid(row=1, column=0, sticky="w")
        self.combo = ttk.Combobox(self.marco, state="readonly", width=42)
        self.combo.grid(row=0, column=1, rowspan=2, sticky="e", padx=(16, 0))
        self.combo.bind("<<ComboboxSelected>>", lambda _e: ventana.elegir(self))
        # Tk cambia la opción con la rueda del mouse: la rueda solo desplaza la lista.
        self.combo.bind("<MouseWheel>", ventana._rueda)
        self.marco.columnconfigure(0, weight=1)


class Ventana:
    INTERVALO_MS = 1500  # cada cuánto se revisa si cambió algo

    def __init__(self, raiz):
        self.raiz = raiz
        self.politica = PoliticaAudio()
        self.salidas, self.predeterminada = None, None
        self.filas = {}
        self._estilo()

        cuerpo = ttk.Frame(raiz, padding=20)
        cuerpo.pack(fill="both", expand=True)
        cuerpo.columnconfigure(0, weight=1)
        ttk.Label(cuerpo, text="Audio por app", style="Titulo.TLabel").grid(sticky="w")
        ttk.Label(cuerpo, text="Elige por qué salida suena cada programa.",
                  style="Tenue.TLabel").grid(sticky="w", pady=(0, 16))

        self.titulo_salidas = ttk.Label(cuerpo, style="Seccion.TLabel")
        self.titulo_salidas.grid(sticky="w")
        self.lista_salidas = ttk.Label(cuerpo, justify="left")
        self.lista_salidas.grid(sticky="w", pady=(4, 18))

        ttk.Label(cuerpo, text="PROGRAMAS CON AUDIO", style="Seccion.TLabel").grid(sticky="w", pady=(0, 6))
        # Con muchos programas la lista se desplaza en vez de salirse de la pantalla.
        marco = ttk.Frame(cuerpo)
        marco.grid(sticky="ew")
        marco.columnconfigure(0, weight=1)
        self.lienzo = tk.Canvas(marco, bg=FONDO, highlightthickness=0, bd=0)
        self.lienzo.grid(row=0, column=0, sticky="ew")
        self.barra = ttk.Scrollbar(marco, orient="vertical", command=self.lienzo.yview)
        self.lienzo.configure(yscrollcommand=self.barra.set)
        self.lista = ttk.Frame(self.lienzo)
        self.lista.columnconfigure(0, weight=1)
        hueco = self.lienzo.create_window(0, 0, window=self.lista, anchor="nw")
        self.lienzo.bind("<Configure>", lambda e: self.lienzo.itemconfigure(hueco, width=e.width))
        self.lista.bind("<Configure>", lambda e: self._ajustar_lista())
        raiz.bind_all("<MouseWheel>", self._rueda)
        self.vacio = ttk.Label(cuerpo, style="Tenue.TLabel",
                               text="Ningún programa tiene el audio abierto. Pon algo a sonar y aparece aquí.")
        self.vacio.grid(sticky="w")

        pie = ttk.Frame(cuerpo)
        pie.grid(sticky="ew", pady=(18, 0))
        pie.columnconfigure(0, weight=1)
        self.aviso = ttk.Label(pie, style="Tenue.TLabel",
                               text="Si un juego no cambia de salida, reinícialo: algunos la leen solo al abrir.")
        self.aviso.grid(row=0, column=0, sticky="w")
        ttk.Button(pie, text="Todo a la de Windows", command=self.restablecer).grid(row=0, column=1, padx=(12, 0))

        self.refrescar()

    def _estilo(self):
        r = self.raiz
        r.configure(bg=FONDO)
        s = ttk.Style(r)
        s.theme_use("clam")
        s.configure(".", background=FONDO, foreground=TEXTO, font=("Segoe UI", 10))
        s.configure("Titulo.TLabel", font=("Segoe UI Semibold", 16))
        s.configure("Tenue.TLabel", foreground=TENUE)
        s.configure("Seccion.TLabel", foreground=TENUE, font=("Segoe UI Semibold", 9))
        s.configure("Tarjeta.TFrame", background=TARJETA)
        s.configure("Nombre.TLabel", background=TARJETA, font=("Segoe UI Semibold", 11))
        s.configure("Callado.TLabel", background=TARJETA, foreground=TENUE, font=("Segoe UI", 9))
        s.configure("Sonando.TLabel", background=TARJETA, foreground=ACENTO, font=("Segoe UI", 9))
        s.configure("TCombobox", fieldbackground=CAMPO, background=CAMPO, foreground=TEXTO, arrowcolor=TEXTO,
                    bordercolor=BORDE, lightcolor=CAMPO, darkcolor=CAMPO, padding=6)
        s.map("TCombobox", fieldbackground=[("readonly", CAMPO)], foreground=[("readonly", TEXTO)],
              selectbackground=[("readonly", CAMPO)], selectforeground=[("readonly", TEXTO)],
              background=[("active", BORDE)])
        s.configure("TButton", background=CAMPO, foreground=TEXTO, bordercolor=BORDE,
                    lightcolor=CAMPO, darkcolor=CAMPO, padding=(12, 6))
        s.map("TButton", background=[("active", BORDE)])
        s.configure("Vertical.TScrollbar", background=CAMPO, troughcolor=FONDO, bordercolor=FONDO,
                    lightcolor=CAMPO, darkcolor=CAMPO, arrowcolor=TEXTO)
        r.option_add("*TCombobox*Listbox.background", CAMPO)
        r.option_add("*TCombobox*Listbox.foreground", TEXTO)
        r.option_add("*TCombobox*Listbox.selectBackground", BORDE)
        r.option_add("*TCombobox*Listbox.font", "{Segoe UI} 10")

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
            n = len(salidas)
            self.titulo_salidas.configure(text=f"{n} SALIDA{'S' if n != 1 else ''} DETECTADA{'S' if n != 1 else ''}")
            self.lista_salidas.configure(text="\n".join(
                f"•  {nombre}" + ("     predeterminada de Windows" if i == predeterminada else "")
                for i, nombre in salidas) or "No hay ninguna salida de audio conectada.")
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
                f = self.filas[clave] = Fila(self.lista, self, nombre)
                f.combo["values"] = self._opciones()
            f.pids, f.sonando = sorted(pids), sonando
            f.estado.configure(text="● sonando" if sonando else "en silencio",
                               style="Sonando.TLabel" if sonando else "Callado.TLabel")
            asignada = self._asignada(f.pids)
            if asignada != f.asignada:
                f.asignada = asignada
                f.combo.set(self._texto(asignada))

        orden = sorted(self.filas.values(), key=lambda f: (not f.sonando, f.nombre.lower()))
        for i, f in enumerate(orden):
            f.marco.grid(row=i, column=0, sticky="ew", pady=(0, 6))
        if self.filas:
            self.vacio.grid_remove()
        else:
            self.vacio.grid()
        self._ajustar_lista()

    def _ajustar_lista(self):
        alto = self.lista.winfo_reqheight() if self.filas else 0
        tope = self.raiz.winfo_screenheight() // 2
        self.lienzo.configure(width=self.lista.winfo_reqwidth(), height=max(min(alto, tope), 1),
                              scrollregion=(0, 0, 0, alto))
        if alto > tope:
            self.barra.grid(row=0, column=1, sticky="ns", padx=(6, 0))
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
        self.politica.limpiar_todo()
        for f in self.filas.values():
            f.asignada = DESCONOCIDA
        self.aviso.configure(text="Todos los programas vuelven a la salida de Windows.")


def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # texto nítido en pantallas con escala
    except (AttributeError, OSError):
        pass
    raiz = tk.Tk()
    raiz.title("Audio por app")
    raiz.minsize(600, 0)
    try:  # barra de título oscura, a juego con la ventana
        raiz.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(raiz.winfo_id())
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, byref(c_int(1)), ctypes.sizeof(c_int))
    except (AttributeError, OSError):
        pass
    try:
        Ventana(raiz)
    except OSError as e:
        messagebox.showerror("Audio por app", str(e))
        return
    raiz.mainloop()


if __name__ == "__main__":
    main()
