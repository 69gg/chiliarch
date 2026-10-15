import type { JsonObject } from "./types";

export function isGitHubEnabled(config: JsonObject): boolean {
  const github = config.github as JsonObject | undefined;
  return github?.enabled !== false;
}
