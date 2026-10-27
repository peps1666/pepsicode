/// <reference types="vite/client" />

interface PepsiAPI {
  getServerPort: () => Promise<number>;
  getServerConnection: () => Promise<{ port: number; token: string }>;
  selectFolder: () => Promise<string | null>;
  showAbout: () => Promise<void>;
}

interface Window {
  pepsiAPI: PepsiAPI;
}

declare global {
  interface Window {
    pepsiAPI: PepsiAPI;
  }
}

export {};
