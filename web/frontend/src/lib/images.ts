import type { Image } from "../types";

/** Repository tags with Docker's "<none>:<none>" placeholder filtered out. */
export function realTags(image: Image | null | undefined): string[] {
  return (image?.repoTags ?? []).filter((t) => t && t !== "<none>:<none>");
}

/** Truncate a string to a maximum length, appending an ellipsis if cut. */
export function truncate(value: string, max = 80): string {
  return value.length > max ? `${value.slice(0, max)}…` : value;
}

/**
 * A loosely-typed view over the raw `docker inspect` JSON for an image. Every
 * field is optional because the agent may omit anything; consumers guard with
 * optional chaining. The `Config` sub-object mirrors Docker's image config.
 */
export interface ImageInspectJSON {
  Id?: string;
  RepoTags?: string[];
  Architecture?: string;
  Os?: string;
  Size?: number;
  Author?: string;
  Created?: string;
  RepoDigests?: string[];
  RootFS?: { Layers?: string[] };
  Config?: {
    Env?: string[];
    Entrypoint?: string[];
    Cmd?: string[];
    WorkingDir?: string;
    User?: string;
    ExposedPorts?: Record<string, unknown>;
    Labels?: Record<string, string>;
  };
}
