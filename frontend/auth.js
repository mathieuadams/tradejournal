// Cognito hosted sign-in with authorization code + PKCE. No secrets in the browser.
(function () {
  const C = window.TJ_CONFIG;
  const KEY = 'tj.auth';
  const redirect = location.origin + '/';
  const b64url = buf => btoa(String.fromCharCode(...new Uint8Array(buf))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  const rand = n => b64url(crypto.getRandomValues(new Uint8Array(n)));
  const decode = jwt => JSON.parse(atob(jwt.split('.')[1].replace(/-/g, '+').replace(/_/g, '/')));
  const read = () => { try { return JSON.parse(localStorage.getItem(KEY)) } catch (e) { return null } };
  const write = v => { try { v ? localStorage.setItem(KEY, JSON.stringify(v)) : localStorage.removeItem(KEY) } catch (e) {} };

  async function tokenRequest(params) {
    const r = await fetch(`https://${C.cognitoDomain}/oauth2/token`, {
      method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({ client_id: C.clientId, ...params })
    });
    if (!r.ok) throw new Error('Sign-in failed (' + r.status + '). Try again.');
    return r.json();
  }
  function store(t, refresh) {
    const exp = decode(t.id_token).exp * 1000;
    const claims = decode(t.id_token);
    write({ id: t.id_token, refresh: t.refresh_token || refresh, exp, email: claims.email });
  }

  window.Auth = {
    async login() {
      const verifier = rand(48);
      const challenge = b64url(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier)));
      const state = rand(16);
      sessionStorage.setItem('tj.pkce', JSON.stringify({ verifier, state }));
      const q = new URLSearchParams({ response_type: 'code', client_id: C.clientId, redirect_uri: redirect,
        scope: 'openid email profile', code_challenge_method: 'S256', code_challenge: challenge, state });
      location.assign(`https://${C.cognitoDomain}/oauth2/authorize?${q}`);
    },
    async handleCallback() {
      const p = new URLSearchParams(location.search);
      if (p.get('error')) { history.replaceState({}, '', '/'); throw new Error(p.get('error_description') || p.get('error')); }
      if (!p.get('code')) return false;
      const saved = JSON.parse(sessionStorage.getItem('tj.pkce') || '{}');
      sessionStorage.removeItem('tj.pkce');
      history.replaceState({}, '', '/' + location.hash);
      if (!saved.state || saved.state !== p.get('state')) throw new Error('Sign-in expired. Try again.');
      const t = await tokenRequest({ grant_type: 'authorization_code', code: p.get('code'), redirect_uri: redirect, code_verifier: saved.verifier });
      store(t);
      return true;
    },
    async token() {
      const a = read();
      if (!a) return null;
      if (Date.now() < a.exp - 60000) return a.id;
      if (!a.refresh) { write(null); return null; }
      try { const t = await tokenRequest({ grant_type: 'refresh_token', refresh_token: a.refresh }); store(t, a.refresh); return read().id; }
      catch (e) { write(null); return null; }
    },
    email() { return (read() || {}).email || ''; },
    logout() {
      write(null);
      location.assign(`https://${C.cognitoDomain}/logout?client_id=${C.clientId}&logout_uri=${encodeURIComponent(redirect)}`);
    }
  };
})();
