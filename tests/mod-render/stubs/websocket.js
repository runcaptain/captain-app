// The kernel socket in render tests: globalThis.__kernel(msg, send) answers each written message, as the backend's
// kernel would, by calling send({...}) for every reply. Connections are recorded in globalThis.__sockets.
export const WebSocket = {
  connect: async (url) => {
    const inbox = [];
    let wake = null;
    const send = (m) => { inbox.push(JSON.stringify(m)); wake?.(); wake = null; };
    const sock = {
      url,
      closed: false,
      async read() {
        while (!inbox.length) {
          if (sock.closed) throw new Error('closed');
          await new Promise((r) => (wake = r));
        }
        return inbox.shift();
      },
      async write(data) { setTimeout(() => globalThis.__kernel?.(JSON.parse(data), send), 0); },
      async close() { sock.closed = true; wake?.(); },
    };
    (globalThis.__sockets ??= []).push(sock);
    return sock;
  },
};
