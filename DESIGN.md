# Kowan design system

Updated from the hierarchy prototype approved on 27 October 2026.

## Direction

A quiet operational console. Each screen answers one question first: what needs attention, what is running, or what is happening to this workload. Names, states and the next useful action lead. Configuration, identifiers and historical detail follow.

Keep Space Grotesk, the Kowan seal, square controls and the existing palette system. Hierarchy comes from type size, weight, whitespace and placement. Do not frame every heading or turn every fact into a card.

## Hierarchy and layout

- Page titles: 30px desktop, 28px mobile; natural case, weight 650. Object names retain their spelling.
- Section headings: 18px, weight 600. Body and table content: 14px. Supporting labels: 12–13px, normal case and normal tracking.
- Space Grotesk for readable text, labels, navigation and states. Geist Mono for code, image references, identifiers, ports and command output.
- Centre the content within a 1440px maximum; use 40px desktop gutters, 28px on medium screens and 20px on phones. Separate major sections by 28–32px.
- One clear primary action. Lifecycle and secondary actions live in an explicit Actions menu; dangerous operations retain their confirmations and permission checks.
- A soft semantic tint and a readable label communicate state. Red identifies failure, amber requires attention, green indicates healthy/running, gray indicates stopped or unknown. Runtime health and deployment outcome are separate facts.
- Light and dark modes use the existing theme tokens. Flat page canvas, quiet separators, surfaces only where grouping or interaction needs them.

## Navigation

Overview, Workloads and Hosts are direct destinations. Resources, Automation and Monitoring expand to reveal their destinations; the active group stays open. Settings has its own sidebar and a Back destination. Host scope appears once in the top bar; server settings are labelled as server-wide. The command palette remains a shortcut to all enabled destinations.

## Screen patterns

Overview puts recent attention items first, hosts second and recent activity next. Each attention row has a short title and a relevant action; its explanation expands. Older container failures are grouped separately. Offline states remain explicit.

Workloads defaults to one table with Workload, State, Host and Attention columns. Search and filters narrow that same list. Workload names lead, kind and management metadata follow. Service and container rows expand on demand. Card view remains available. On mobile, each row keeps its name and state together, with host and attention underneath.

Workload detail puts the real name and runtime state in the header, View logs first, lifecycle controls in Actions. Containers and current CPU/memory lead the overview. Resource history, update policy, backups, schedules, exposure and deployment settings expand when needed. Metadata is a quiet side column on large screens and follows the content on smaller screens. A single service starts expanded; larger groups and remembered choices retain their own state.

Forms and resource/monitoring/settings pages share plain page headers, clear section headings, subdued descriptions and the same controls. Multi-value policies use explicit selects; binary policies use switches. Unknown or loading data must not be presented as an empty or disabled feature.

## Accessibility and responsiveness

Keep semantic headings, labelled controls, native disclosure keyboard behavior, tab keyboard navigation and an instant visible focus ring. Touch actions reach 44px. Technical tables may scroll when necessary; workload lists and container summaries reflow on phones. Never shrink a page title to make room for actions: actions move to a new row.

Use opacity and transform for motion; respect reduced-motion preferences. Content is immediately readable, without a page entrance animation.

## Implementation

Shared rules live in `web/frontend/src/styles/hierarchy.css`, with theme and base tokens in `globals.css` and `themes.css`. `PageHeader`, `SectionCard`, `FormSection`, the UI kit and the shell apply the system across routes. Workload and overview components provide their denser operational layouts. Existing navigation, role restrictions, editor flows and confirmation dialogs remain part of the product contract.
