export interface SlashCommandInfo {
  name: string;
  usage: string;
  description: string;
  source: string;
}

export function matchingCommands(commands: SlashCommandInfo[], input: string): SlashCommandInfo[] {
  if (!/^\/[^\s]*$/.test(input)) return [];
  const names = new Set<string>();
  return commands.filter((command) => {
    if (!command.name.startsWith(input) || names.has(command.name)) return false;
    names.add(command.name);
    return true;
  }).slice(0, 8);
}

export function commandDraft(command: SlashCommandInfo): string {
  return command.name + (command.usage === command.name ? "" : " ");
}
