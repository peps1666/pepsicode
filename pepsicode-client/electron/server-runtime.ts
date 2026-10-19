// Preserve user-defined provider variables while excluding Electron runtime
// settings that must not affect Python or its Node/MCP descendants.
export function pythonEnvironment(source: NodeJS.ProcessEnv): NodeJS.ProcessEnv {
  return Object.fromEntries(Object.entries(source).filter(([key]) =>
    !/^(ELECTRON_|CHROME_|CHROMIUM_|CRASHPAD_|NODE_OPTIONS$|NODE_INSPECT_RESUME_ON_START$)/i.test(key)
  ));
}

export function trustedRenderer(candidate: string, expected: string): boolean {
  try {
    const actual = new URL(candidate);
    const wanted = new URL(expected);
    return wanted.protocol === "file:"
      ? actual.protocol === "file:" && actual.host === wanted.host && actual.pathname === wanted.pathname
      : actual.origin === wanted.origin;
  } catch { return false; }
}

export function serverOrigins(renderer: string): string[] {
  const origin = new URL(renderer).origin;
  return origin === "null" ? ["null", "file://"] : ["null", "file://", origin];
}
