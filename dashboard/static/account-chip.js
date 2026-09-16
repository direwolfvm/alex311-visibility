// The account chip in the site bar, shared by every page.
//
// One script, one fetch of /submit/api/whoami, one place the answer is drawn:
// a "Sign in" link for a visitor, and for a signed-in person a chip with their
// initial and label that opens a small menu — My requests, Account, Admin for
// administrators, Sign out. The pages used to each draw their own "name ·
// Sign out" text; this replaces those, and also reveals the My requests and
// Admin links in the bar, which the pages used to do one by one.
//
// A page includes it with <script src="/account-chip.js" defer></script> and
// gives the header a <div id="account"></div>; without the placeholder the
// chip is appended to the header. Other scripts on the page may await
// window.alex311.whoami instead of asking again.
(() => {
  const css = `
  header .account { margin-left:auto; position:relative; display:flex; align-items:center; align-self:center; }
  header .account .chip { display:inline-flex; align-items:center; gap:8px; max-width:260px;
    padding:4px 10px 4px 4px; border:1px solid var(--border); border-radius:99px; background:var(--panel);
    color:var(--ink); font:inherit; font-size:13px; cursor:pointer; text-decoration:none; line-height:1.2; }
  header .account .chip:hover, header .account .chip[aria-expanded="true"] { border-color:var(--accent); color:var(--accent); }
  header .account .chip:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }
  header .account .chip.chip-in { padding:6px 14px; color:var(--accent); font-weight:600; }
  header .account .avatar { width:24px; height:24px; border-radius:50%; background:var(--accent); color:#fff;
    font-size:12px; font-weight:700; display:inline-flex; align-items:center; justify-content:center; flex:none; }
  header .account .name { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  header .account .caret { font-size:10px; color:var(--muted); }
  header .account .menu { position:absolute; right:0; top:calc(100% + 6px); min-width:220px; z-index:2000;
    background:var(--panel); border:1px solid var(--border); border-radius:10px; padding:6px;
    box-shadow:0 8px 24px rgba(28,39,51,.12); }
  header .account .menu[hidden] { display:none; }
  header .account .menu-head { padding:8px 10px 10px; border-bottom:1px solid var(--border); margin-bottom:6px; }
  header .account .menu-head b { display:block; font-size:13px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  header .account .menu-head span { font-size:11px; color:var(--muted); text-transform:uppercase; letter-spacing:.04em; }
  header .account .menu a { display:block; padding:8px 10px; border-radius:7px; color:var(--ink);
    text-decoration:none; font-size:14px; }
  header .account .menu a:hover, header .account .menu a:focus-visible { background:var(--bg); color:var(--accent); outline:none; }
  header .account .menu a.out { color:var(--muted); border-top:1px solid var(--border); border-radius:0 0 7px 7px; margin-top:6px; }
  @media (max-width:640px) {
    header .sub { order:1; }
    header .account { order:0; margin-left:auto; }
    header .account .name { max-width:120px; }
  }
  @media (pointer:coarse) { header .account .menu a { padding:11px 10px; } }`;

  const style = document.createElement('style');
  style.textContent = css;
  document.head.appendChild(style);

  const header = document.querySelector('header');
  let host = document.getElementById('account');
  if (!host && header) { host = document.createElement('div'); host.id = 'account'; header.appendChild(host); }
  if (!host) return;
  host.className = 'account';

  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;          // never innerHTML: the label is user data
    return n;
  };
  const initial = label => {
    const m = /[A-Za-z0-9]/.exec(label || '');
    return m ? m[0].toUpperCase() : '?';
  };

  const whoami = fetch('/submit/api/whoami')
    .then(r => r.ok ? r.json() : {user: null})
    .catch(() => ({user: null}));
  window.alex311 = Object.assign(window.alex311 || {}, {whoami});

  function drawVisitor() {
    const a = el('a', 'chip chip-in', 'Sign in');
    a.href = '/submit/login';
    host.replaceChildren(a);
  }

  function drawUser(user) {
    const label = user.label || user.email || 'account';
    const admin = user.role === 'admin';

    const btn = el('button', 'chip');
    btn.type = 'button';
    btn.setAttribute('aria-haspopup', 'menu');
    btn.setAttribute('aria-expanded', 'false');
    btn.setAttribute('aria-controls', 'account-menu');
    btn.setAttribute('aria-label', `Account: ${label}`);
    const av = el('span', 'avatar', initial(label)); av.setAttribute('aria-hidden', 'true');
    const caret = el('span', 'caret', '▾'); caret.setAttribute('aria-hidden', 'true');
    btn.append(av, el('span', 'name', label), caret);

    const menu = el('div', 'menu');
    menu.id = 'account-menu';
    menu.setAttribute('role', 'menu');
    menu.setAttribute('aria-label', 'Account');
    menu.hidden = true;
    const head = el('div', 'menu-head');
    head.append(el('b', null, label), el('span', null, admin ? 'Administrator' : 'Signed in'));
    menu.appendChild(head);
    const items = [['My requests', '/submit/my'], ['Account', '/submit/account']];
    if (admin) items.push(['Admin', '/submit/admin']);
    for (const [text, href] of items) {
      const a = el('a', null, text); a.href = href; a.setAttribute('role', 'menuitem'); a.tabIndex = -1;
      if (location.pathname === href) a.setAttribute('aria-current', 'page');
      menu.appendChild(a);
    }
    const out = el('a', 'out', 'Sign out'); out.href = '/submit/logout';
    out.setAttribute('role', 'menuitem'); out.tabIndex = -1;
    menu.appendChild(out);

    const links = () => [...menu.querySelectorAll('a')];
    const open = focusFirst => {
      menu.hidden = false; btn.setAttribute('aria-expanded', 'true');
      if (focusFirst) links()[0].focus();
    };
    const close = refocus => {
      if (menu.hidden) return;
      menu.hidden = true; btn.setAttribute('aria-expanded', 'false');
      if (refocus) btn.focus();
    };
    btn.addEventListener('click', () => menu.hidden ? open(false) : close(false));
    btn.addEventListener('keydown', e => {
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { e.preventDefault(); open(true); }
    });
    menu.addEventListener('keydown', e => {
      const all = links(); const i = all.indexOf(document.activeElement);
      if (e.key === 'ArrowDown') { e.preventDefault(); all[(i + 1) % all.length].focus(); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); all[(i - 1 + all.length) % all.length].focus(); }
      else if (e.key === 'Home') { e.preventDefault(); all[0].focus(); }
      else if (e.key === 'End') { e.preventDefault(); all[all.length - 1].focus(); }
      else if (e.key === 'Tab') { close(false); }
    });
    document.addEventListener('keydown', e => { if (e.key === 'Escape') close(true); });
    document.addEventListener('click', e => { if (!host.contains(e.target)) close(false); });

    host.replaceChildren(btn, menu);
  }

  whoami.then(({user}) => {
    const my = document.getElementById('nav-my');
    const admin = document.getElementById('nav-admin');
    if (user && my) my.hidden = false;
    if (user && user.role === 'admin' && admin) admin.hidden = false;
    user ? drawUser(user) : drawVisitor();
  });
})();
