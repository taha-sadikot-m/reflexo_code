"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { api, type Admin } from "@/lib/api";
import { useApp } from "@/components/Providers";
import { Brandmark } from "@/components/admin/Brandmark";
import { Badge, Button, Field, Input } from "@/components/ui";
import { Icon } from "@/components/Icon";
import { loginSchema, validationErrors, type FieldErrors } from "@/lib/schemas";

export default function LoginPage() {
  const router = useRouter();
  const { setAdmin } = useApp();
  const [email, setEmail] = useState("taha@reflexo.ai");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [fieldErrors, setFieldErrors] = useState<FieldErrors>({});
  const [pending, setPending] = useState(false);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    const parsed = loginSchema.safeParse({ email, password });
    if (!parsed.success) {
      setFieldErrors(validationErrors(parsed.error));
      return;
    }
    setPending(true);
    setError("");
    setFieldErrors({});
    try {
      const res = await api<{ access_token: string; admin: Admin }>("/api/admin/auth/login", {
        method: "POST",
        body: JSON.stringify(parsed.data),
      });
      sessionStorage.setItem("reflexo_admin_token", res.access_token);
      setAdmin(res.admin);
      router.replace("/dashboard");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Login failed");
    } finally {
      setPending(false);
    }
  }

  return (
    <section className="login">
      <div className="login-hero">
        <Brandmark />
        <div className="login-hero-mid">
          <Badge tone="accent">Internal operations · Concept A &ldquo;Handloom&rdquo;</Badge>
          <h1>Every AI employee, <em>woven</em> into one view.</h1>
          <p>Inspect routing decisions, correct knowledge, approve destructive actions, and rehearse any customer conversation before it happens.</p>
          <div className="login-proof">
            <div><b className="num">42</b><span>Businesses</span></div>
            <div><b className="num">1.28k</b><span>Msgs today</span></div>
            <div><b className="num">1.8s</b><span>Median reply</span></div>
          </div>
        </div>
      </div>
      <div className="login-form-side">
        <form className="login-form" onSubmit={onSubmit}>
          <Brandmark compact />
        <h2>Sign in</h2>
          <p className="muted login-intro">Staff accounts only. Access is logged and scoped by role.</p>
          <div className="fields">
            <Field label="Work email" required error={fieldErrors.email}>
              <span className="input-affix">
                <Icon name="users" className="ic ic-sm" />
                <Input
                  value={email}
                  onChange={(event) => setEmail(event.target.value)}
                  type="email"
                  autoComplete="username"
                  required
                />
              </span>
            </Field>
            <Field label="Password" required error={fieldErrors.password}>
              <span className="input-affix">
                <Icon name="lock" className="ic ic-sm" />
                <Input
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
                  type="password"
                  autoComplete="current-password"
                  required
                />
              </span>
            </Field>
            <div className="row-flex gap2 muted login-session-note">
              <Icon name="shield" className="ic ic-sm" />
              <span>Protected 8-hour staff session</span>
            </div>
            {error && <div className="alert alert-neg" role="alert"><Icon name="alert" /><div><b>Sign-in failed</b><p>{error}</p></div></div>}
            <Button variant="primary" size="lg" block type="submit" loading={pending}>
              Sign in to Control Room
            </Button>
          </div>
          <hr className="thread-rule mt6" />
          <p className="faint tc mt4 login-security">Protected REFLEXO administration.</p>
        </form>
      </div>
    </section>
  );
}
