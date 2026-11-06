// Example Captain Search Tools plugin.
// Install: copy this folder to ~/.config/captain-search/plugins/hello/ and restart the app.
// The default export receives { registerPanel, registerCommand, api, plugin }.
// `api(path, init)` calls the local daemon with its token, like the app does.

export default function hello({ registerPanel, registerCommand, api }) {
  registerPanel({
    id: 'hello',
    title: 'Hello',
    icon: 'sparkles',
    render(el) {
      el.innerHTML = '<p style="font:14px Inter,sans-serif;color:#4B515C">Loading the workspace...</p>';
      let alive = true;
      Promise.all([api('/api/workspace'), api('/api/datasets')])
        .then(([ws, datasets]) => {
          if (!alive) return;
          const kinds = {};
          for (const d of datasets) kinds[d.kind] = (kinds[d.kind] || 0) + 1;
          el.innerHTML = `
            <div style="font:14px Inter,sans-serif;color:#000F2E;display:flex;flex-direction:column;gap:12px;max-width:560px">
              <p>Hello from a plugin. This panel is plain DOM, rendered by <code>plugins/examples/hello/index.js</code>.</p>
              <div style="border:1px solid #DCDCDC;padding:16px">
                <div style="font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:#4B515C">Workspace</div>
                <div style="font:500 16px 'JetBrains Mono',monospace;margin-top:6px" data-testid="hello-workspace">${ws.org}/${ws.name}</div>
                <div style="color:#4B515C;margin-top:4px">${datasets.length} datasets: ${Object.entries(kinds).map(([k, n]) => `${n} ${k}`).join(', ')}</div>
              </div>
            </div>`;
        })
        .catch((e) => {
          if (alive) el.textContent = `The plugin could not reach the daemon: ${e.message}`;
        });
      return () => {
        alive = false;
      };
    },
  });

  registerCommand({
    id: 'health',
    title: 'Check daemon health',
    async run() {
      const h = await api('/api/health');
      window.alert(`cst serve ${h.version}: ${h.ok ? 'healthy' : 'not healthy'}`);
    },
  });
}
