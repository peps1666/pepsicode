import { contextBridge, ipcRenderer } from "electron";

contextBridge.exposeInMainWorld("pepsiAPI", {
  getServerPort: (): Promise<number> => ipcRenderer.invoke("get-server-port"),
  selectFolder: (): Promise<string | null> => ipcRenderer.invoke("select-folder"),
});
