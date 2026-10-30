"""Keep Windows default *playback* on a real speaker (not VB-CABLE Input).

VB-CABLE install often makes Windows set CABLE Input as the default render
device. Meeting apps then play the interviewer into the cable — IronStack's
headset loopback goes silent, and with client-mic mode (loopback-only return
mix) the Viewer hears nothing.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from logsetup import get_logger

# PolicyConfig + MMDeviceEnumerator helper. SetDefaultEndpoint lives after ten
# unused slots on IPolicyConfig (Win10/11).
_PS_HELPER = r'''
$ErrorActionPreference = "Stop"
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;

public enum EDataFlow { eRender = 0, eCapture = 1, eAll = 2 }
public enum ERole { eConsole = 0, eMultimedia = 1, eCommunications = 2 }
[Flags] public enum DEVICE_STATE { ACTIVE = 0x00000001 }

[Guid("D666063F-1587-4E43-81F1-B948E807363F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IMMDevice {
    void Activate(ref Guid iid, int dwClsCtx, IntPtr pActivationParams,
        [MarshalAs(UnmanagedType.IUnknown)] out object ppInterface);
    void OpenPropertyStore(int stgmAccess, out IPropertyStore ppProperties);
    void GetId([MarshalAs(UnmanagedType.LPWStr)] out string ppstrId);
    void GetState(out int pdwState);
}

[Guid("0BD7A1BE-7A1A-44DB-8397-CC5392387B5E"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IMMDeviceCollection {
    void GetCount(out uint pcDevices);
    void Item(uint nDevice, out IMMDevice ppDevice);
}

[Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IMMDeviceEnumerator {
    void EnumAudioEndpoints(EDataFlow dataFlow, DEVICE_STATE dwStateMask,
        out IMMDeviceCollection ppDevices);
    void GetDefaultAudioEndpoint(EDataFlow dataFlow, ERole role, out IMMDevice ppEndpoint);
    void GetDevice(string pwstrId, out IMMDevice ppDevice);
    void RegisterEndpointNotificationCallback(IntPtr pClient);
    void UnregisterEndpointNotificationCallback(IntPtr pClient);
}

[ComImport, Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")]
public class MMDeviceEnumeratorCom { }

[Guid("886d8eeb-8cf2-4446-8d02-cdba1dbdcf99"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IPropertyStore {
    void GetCount(out uint cProps);
    void GetAt(uint iProp, out PROPERTYKEY pkey);
    void GetValue(ref PROPERTYKEY key, out PROPVARIANT pv);
    void SetValue(ref PROPERTYKEY key, ref PROPVARIANT pv);
    void Commit();
}

[StructLayout(LayoutKind.Sequential)]
public struct PROPERTYKEY { public Guid fmtid; public uint pid; }

[StructLayout(LayoutKind.Sequential)]
public struct PROPVARIANT {
    public ushort vt; public ushort w1; public ushort w2; public ushort w3; public IntPtr data;
}

[ComImport, Guid("870af99c-171d-4f9e-af0d-e63df40c2bc9")]
public class CPolicyConfigClient { }

[Guid("f8679f50-850a-41cf-9c72-430f290290c8"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IPolicyConfig {
    void Unused1(); void Unused2(); void Unused3(); void Unused4();
    void Unused5(); void Unused6(); void Unused7(); void Unused8();
    void Unused9(); void Unused10();
    [PreserveSig] int SetDefaultEndpoint(
        [MarshalAs(UnmanagedType.LPWStr)] string deviceId, ERole role);
}

public static class AudioDefaults {
    static readonly PROPERTYKEY NameKey = new PROPERTYKEY {
        fmtid = new Guid("a45c254e-df1c-4efd-8020-67d146a850e0"), pid = 14 };

    public static string GetDefaultRenderName() {
        var en = (IMMDeviceEnumerator)(new MMDeviceEnumeratorCom());
        IMMDevice dev;
        en.GetDefaultAudioEndpoint(EDataFlow.eRender, ERole.eMultimedia, out dev);
        return FriendlyName(dev);
    }

    public static string FindRealRenderId(string preferred) {
        var en = (IMMDeviceEnumerator)(new MMDeviceEnumeratorCom());
        IMMDeviceCollection col;
        en.EnumAudioEndpoints(EDataFlow.eRender, DEVICE_STATE.ACTIVE, out col);
        uint n; col.GetCount(out n);
        string fallback = null;
        preferred = (preferred ?? "").ToLowerInvariant();
        for (uint i = 0; i < n; i++) {
            IMMDevice d; col.Item(i, out d);
            string name = FriendlyName(d);
            if (string.IsNullOrEmpty(name) || IsVirtual(name)) continue;
            string id; d.GetId(out id);
            if (!string.IsNullOrEmpty(preferred) &&
                name.ToLowerInvariant().Contains(preferred))
                return id;
            if (fallback == null) fallback = id;
        }
        return fallback;
    }

    public static int SetDefault(string id) {
        var cfg = (IPolicyConfig)(new CPolicyConfigClient());
        int hr = 0;
        hr |= cfg.SetDefaultEndpoint(id, ERole.eConsole);
        hr |= cfg.SetDefaultEndpoint(id, ERole.eMultimedia);
        hr |= cfg.SetDefaultEndpoint(id, ERole.eCommunications);
        return hr;
    }

    static bool IsVirtual(string name) {
        var n = name.ToLowerInvariant();
        return n.Contains("cable input") || n.Contains("voicemeeter input")
            || n.Contains("voicemeeter aux input") || n.Contains("blackhole");
    }

    static string FriendlyName(IMMDevice d) {
        IPropertyStore ps; d.OpenPropertyStore(0, out ps);
        PROPERTYKEY key = NameKey; PROPVARIANT pv;
        ps.GetValue(ref key, out pv);
        return Marshal.PtrToStringUni(pv.data) ?? "";
    }
}
"@

$preferred = $env:IRONSTACK_AUDIO_PREFERRED
$cur = [AudioDefaults]::GetDefaultRenderName()
Write-Output ("CURRENT=" + $cur)
$n = $cur.ToLowerInvariant()
$virtual = $n.Contains("cable input") -or $n.Contains("voicemeeter input") -or $n.Contains("blackhole")
if (-not $virtual -and $cur) {
    Write-Output ("OK=" + $cur)
    exit 0
}
$id = [AudioDefaults]::FindRealRenderId($preferred)
if (-not $id) { Write-Output "ERROR=no-real-device"; exit 1 }
$hr = [AudioDefaults]::SetDefault($id)
$after = [AudioDefaults]::GetDefaultRenderName()
Write-Output ("RESTORED=" + $after)
Write-Output ("HR=" + $hr)
'''


def ensure_real_playback_default(preferred_name: str = "") -> bool:
    """If Windows default playback is a virtual cable, restore a real speaker.

    Returns True when the default is (now) a real playback device.
    """
    if sys.platform != "win32":
        return True
    try:
        with tempfile.TemporaryDirectory(prefix="ironstack-audio-") as tmp:
            script = Path(tmp) / "fix_default.ps1"
            script.write_text(_PS_HELPER, encoding="utf-8")
            env = os.environ.copy()
            env["IRONSTACK_AUDIO_PREFERRED"] = preferred_name or ""
            # CREATE_NO_WINDOW — avoid a flashing console on the Host.
            creationflags = 0x08000000 if sys.platform == "win32" else 0
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-File", str(script)],
                capture_output=True, text=True, timeout=30, env=env,
                creationflags=creationflags)
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        for line in out.splitlines():
            get_logger().info("audio: default-playback: %s", line)
        if proc.returncode != 0:
            get_logger().warning(
                "audio: default-playback helper failed (rc=%s): %s",
                proc.returncode, err or out)
            return False
        return any(
            line.startswith("OK=") or line.startswith("RESTORED=")
            for line in out.splitlines())
    except Exception:
        get_logger().exception("audio: could not verify/restore default playback")
        return False
