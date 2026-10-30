import { Fragment } from "react";
import { Badge, Box, Icon, Text } from "@/ui/kit";
import PageHeader from "../components/PageHeader";
import SectionCard from "../components/SectionCard";
import { RELEASES, inlineParts, type Block, type Item, type Release } from "../lib/changelog";

/**
 * What's new: the changelog of this Kowan build, newest first. It is bundled
 * with the SPA, so it describes the version this server runs (marked) and the
 * ones before it; newer releases appear after an update. Each release shows
 * what changes for the user; its other changes (fixes, internal work) fold.
 */
export default function Changelog({ releases = RELEASES, running = __APP_VERSION__ }: { releases?: Release[]; running?: string }) {
  return (
    <Box direction="column" gap={6}>
      <PageHeader
        title="What's new"
        description={`Changes in each Kowan release. This server runs ${running}.`}
        icon={<Icon name="new-releases-outline-rounded" size="lg" />}
      />
      {releases.length ? (
        <SectionCard plain noBodyPadding>
          <ol className="m-0 list-none p-0" aria-label="Releases">
            {releases.map((r) => (
              <li key={r.version} className="grid gap-x-10 gap-y-4 border-b px-6 py-8 last:border-b-0 md:grid-cols-[150px_minmax(0,1fr)]">
                <div className="flex flex-col items-start gap-2 md:sticky md:top-6 md:self-start">
                  <h2 className="m-0 font-mono text-[17px] leading-[24px] font-semibold">{r.version}</h2>
                  {r.date && <time dateTime={r.date} className="text-[12px] text-muted-foreground">{formatDate(r.date)}</time>}
                  {r.version === running && <Badge color="green" size="sm" dot>Running</Badge>}
                </div>
                <div className="flex max-w-[72ch] flex-col gap-6">
                  {r.blocks.length ? <Blocks blocks={r.blocks} /> : <Text size="sm" color="muted">Fixes only.</Text>}
                  {r.other.length > 0 && <OtherChanges blocks={r.other} />}
                </div>
              </li>
            ))}
          </ol>
        </SectionCard>
      ) : (
        <Text color="muted">This build has no changelog.</Text>
      )}
    </Box>
  );
}

/** The fixes and internal work of a release, folded under one line. */
function OtherChanges({ blocks }: { blocks: Block[] }) {
  const n = blocks.reduce((sum, b) => sum + (b.kind === "ul" || b.kind === "ol" ? b.items.length : 1), 0);
  return (
    <details className="group rounded-md border bg-muted/30">
      <summary className="flex cursor-pointer list-none items-center gap-3 px-5 py-3 text-[13px] font-medium text-muted-foreground select-none hover:text-foreground [&::-webkit-details-marker]:hidden">
        <Icon name="arrow-drop-down-rounded" size="sm" className="-rotate-90 transition-transform group-open:rotate-0" />
        Other changes · {n}
      </summary>
      <div className="border-t px-5 py-5">
        <Blocks blocks={blocks} compact />
      </div>
    </details>
  );
}

function Blocks({ blocks, compact = false }: { blocks: Block[]; compact?: boolean }) {
  return (
    <div className={`flex flex-col ${compact ? "gap-4" : "gap-5"}`}>
      {blocks.map((b, i) => {
        if (b.kind === "h") return <h3 key={i} className="m-0 text-[13px] font-semibold text-muted-foreground">{b.text}</h3>;
        if (b.kind === "p") return <p key={i} className="m-0 text-[14px] leading-[22px]"><Inline text={b.text} /></p>;
        return (
          <ul key={i} className={`m-0 flex list-none flex-col p-0 ${compact ? "gap-4" : "gap-5"}`}>
            {b.items.map((it, j) => <Entry key={j} item={it} compact={compact} />)}
          </ul>
        );
      })}
    </div>
  );
}

/** One change: its title on a line, what it means below. */
function Entry({ item, compact }: { item: Item; compact: boolean }) {
  return (
    <li className="flex flex-col gap-1">
      {item.title && <span className={`font-semibold text-foreground ${compact ? "text-[13px] leading-[20px]" : "text-[14px] leading-[22px]"}`}>{item.title}</span>}
      {item.text && (
        <span className={item.title ? `text-muted-foreground ${compact ? "text-[13px] leading-[20px]" : "text-[13.5px] leading-[21px]"}` : "text-[14px] leading-[22px]"}>
          <Inline text={item.text} />
        </span>
      )}
    </li>
  );
}

function Inline({ text }: { text: string }) {
  return (
    <>
      {inlineParts(text).map((p, i) => {
        if (p.kind === "code") return <code key={i} className="rounded bg-muted px-[4px] py-[1px] font-mono text-[12.5px] text-foreground">{p.text}</code>;
        if (p.kind === "bold") return <strong key={i} className="font-semibold text-foreground">{p.text}</strong>;
        if (p.kind === "link") {
          return /^https?:\/\//.test(p.href)
            ? <a key={i} href={p.href} target="_blank" rel="noreferrer" className="text-signal underline underline-offset-2">{p.text}</a>
            : <Fragment key={i}>{p.text}</Fragment>;
        }
        return <Fragment key={i}>{p.text}</Fragment>;
      })}
    </>
  );
}

function formatDate(iso: string): string {
  const d = new Date(`${iso}T00:00:00Z`);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });
}
