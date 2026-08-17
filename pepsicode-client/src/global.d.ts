/// <reference types="vite/client" />

interface PepsiAPI {
  getServerPort: () => Promise<number>;
  selectFolder: () => Promise<string | null>;
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
