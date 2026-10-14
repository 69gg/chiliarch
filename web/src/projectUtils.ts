import type { ProjectFact } from "./projectTypes";

export function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

export function lineValues(value: FormDataEntryValue | null): string[] {
  return String(value ?? "")
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
}

export function latestFacts(facts: ProjectFact[]): ProjectFact[] {
  const latest = new Map<string, ProjectFact>();
  for (const fact of facts)
    if (!latest.has(fact.key) || latest.get(fact.key)!.version < fact.version)
      latest.set(fact.key, fact);
  return [...latest.values()];
}
