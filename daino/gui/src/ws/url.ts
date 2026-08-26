// Derive WebSocket URLs from window.location so same-origin production works
// with no config. In dev, the Vite proxy forwards /ws (ws:true) to the backend.
export function wsUrl(path: string): string {
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  const clean = path.startsWith("/") ? path : `/${path}`;
  return `${proto}//${window.location.host}${clean}`;
}
