"use client";
import { useState } from "react";
import { useRouter } from "next/navigation";
import * as Api from "@/lib/cliente";
import { Campo } from "@/components/ui";

type Pestana = "login" | "registro" | "recuperar";

export default function LoginPage() {
  const router = useRouter();
  const [tab, setTab] = useState<Pestana>("login");
  const [pendiente, setPendiente] = useState(false);
  const [error, setError] = useState("");
  const [aviso, setAviso] = useState("");
  const [cargando, setCargando] = useState(false);
  const [f, setF] = useState({ email: "", password: "", empresa: "", rut: "", pass2: "" });

  const set = (k: string) => (e: React.ChangeEvent<HTMLInputElement>) =>
    setF({ ...f, [k]: e.target.value });

  const login = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(""); setAviso(""); setCargando(true);
    try {
      const r = await Api.auth.login(f.email, f.password);
      Api.setSession(r.access_token, r.refresh_token, r.user);
      router.push(r.user.role === "admin" ? "/admin" : "/dashboard");
    } catch (err) {
      // El backend distingue la solicitud en revisión de la cuenta desactivada
      // (403 CUENTA_DESACTIVADA): en el segundo caso el mensaje ya lo explica.
      if (Api.esApiError(err) && err.code === "ACCOUNT_PENDING") setPendiente(true);
      else setError(Api.mensajeError(err));
    } finally { setCargando(false); }
  };

  const registrar = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    if (f.password !== f.pass2) { setError("Las contraseñas no coinciden"); return; }
    setCargando(true);
    try {
      await Api.auth.register({ empresa: f.empresa, rut: f.rut, email: f.email, password: f.password });
      setPendiente(true);
    } catch (err) { setError(Api.mensajeError(err)); } finally { setCargando(false); }
  };

  const recuperar = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(""); setAviso(""); setCargando(true);
    try {
      // Responde siempre 200 con el mismo mensaje, exista o no la cuenta.
      const r = await Api.auth.olvidePassword(f.email);
      setAviso(r.message);
    } catch (err) { setError(Api.mensajeError(err)); } finally { setCargando(false); }
  };

  if (pendiente) {
    return (
      <main className="flex min-h-screen items-center justify-center p-4">
        <div className="w-full max-w-md rounded-2xl bg-white p-8 text-center shadow-lg">
          <div className="text-4xl">⏳</div>
          <h1 className="mt-3 text-xl font-bold text-brand">Solicitud en revisión</h1>
          <p className="mt-2 text-sm text-slate-600">
            Tu cuenta está siendo revisada por nuestro equipo. Te notificaremos por email al ser aprobada.
          </p>
          <button className="btn-ghost mt-5" onClick={() => setPendiente(false)}>Volver</button>
        </div>
      </main>
    );
  }

  return (
    <main className="flex min-h-screen items-center justify-center bg-gradient-to-br from-brand to-accent p-4">
      <div className="w-full max-w-md rounded-2xl bg-white p-8 shadow-xl">
        <h1 className="text-center text-2xl font-extrabold tracking-tight text-brand">ACREDITTIA</h1>
        <p className="mb-6 text-center text-sm text-slate-500">Acreditación de contratistas sin fricción</p>
        <div className="mb-6 grid grid-cols-2 rounded-lg bg-slate-100 p-1 text-sm font-semibold">
          {(["login", "registro"] as const).map((t) => (
            <button key={t} onClick={() => { setTab(t); setError(""); setAviso(""); }}
              className={`rounded-md py-2 ${tab === t ? "bg-white text-brand shadow" : "text-slate-500"}`}>
              {t === "login" ? "Ingresar" : "Crear cuenta"}
            </button>
          ))}
        </div>
        {error && <p className="mb-4 rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700">{error}</p>}
        {aviso && <p className="mb-4 rounded-lg bg-sky-50 px-3 py-2 text-sm text-sky-800">{aviso}</p>}
        {tab === "login" && (
          <form onSubmit={login} className="space-y-4">
            <Campo etiqueta="Email"><input className="input" type="email" value={f.email} onChange={set("email")} required /></Campo>
            <Campo etiqueta="Contraseña"><input className="input" type="password" value={f.password} onChange={set("password")} required /></Campo>
            <button className="btn-primary w-full py-2.5" disabled={cargando}>
              {cargando ? "Ingresando..." : "Ingresar a la plataforma →"}
            </button>
            <button type="button" className="w-full text-center text-xs text-slate-500 hover:underline"
              onClick={() => { setTab("recuperar"); setError(""); setAviso(""); }}>
              ¿Olvidaste tu contraseña?
            </button>
            <p className="text-center text-xs text-slate-400">Demo: demo@acredittia.cl / Demo2026! · Admin: admin@acredittia.cl / Admin2026!</p>
          </form>
        )}
        {tab === "registro" && (
          <form onSubmit={registrar} className="space-y-4">
            <Campo etiqueta="Razón social"><input className="input" value={f.empresa} onChange={set("empresa")} required /></Campo>
            <Campo etiqueta="RUT empresa"><input className="input" placeholder="76.543.210-9" value={f.rut} onChange={set("rut")} required /></Campo>
            <Campo etiqueta="Email"><input className="input" type="email" value={f.email} onChange={set("email")} required /></Campo>
            <Campo etiqueta="Contraseña"><input className="input" type="password" minLength={8} value={f.password} onChange={set("password")} required /></Campo>
            <Campo etiqueta="Repite la contraseña"><input className="input" type="password" value={f.pass2} onChange={set("pass2")} required /></Campo>
            <button className="btn-primary w-full py-2.5" disabled={cargando}>
              {cargando ? "Creando..." : "Crear cuenta y comenzar →"}
            </button>
          </form>
        )}
        {tab === "recuperar" && (
          <form onSubmit={recuperar} className="space-y-4">
            <p className="text-sm text-slate-600">
              Te enviaremos un enlace para restablecer la contraseña. Tras el
              cambio hay que volver a ingresar en todos los dispositivos.
            </p>
            <Campo etiqueta="Email"><input className="input" type="email" value={f.email} onChange={set("email")} required /></Campo>
            <button className="btn-primary w-full py-2.5" disabled={cargando}>
              {cargando ? "Enviando..." : "Enviar instrucciones"}
            </button>
            <button type="button" className="w-full text-center text-xs text-slate-500 hover:underline"
              onClick={() => { setTab("login"); setError(""); setAviso(""); }}>
              ← Volver a ingresar
            </button>
          </form>
        )}
      </div>
    </main>
  );
}
