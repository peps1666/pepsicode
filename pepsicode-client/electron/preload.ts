import { contextBridge, ipcRenderer } from "electron";

contextBridge.exposeInMainWorld("pepsiAPI", {
  getServerPort: (): Promise<number> => ipcRenderer.invoke("get-server-port"),
  getServerConnection: (): Promise<{port: number; token: string}> => ipcRenderer.invoke("get-server-connection"),
  selectFolder: (): Promise<string | null> => ipcRenderer.invoke("select-folder"),
});
