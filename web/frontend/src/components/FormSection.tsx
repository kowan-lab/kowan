import { Box, Text } from "@/ui/kit";

interface FormSectionProps {
  title: string;
  description?: React.ReactNode;
  icon?: React.ReactNode;
  /** Optional element rendered at the right of the header (e.g. a switch). */
  action?: React.ReactNode;
  children: React.ReactNode;
  /** "card" (default) renders a bordered panel; "plain" a borderless group. */
  variant?: "card" | "plain";
  /**
   * When true (plain only), lay the section out as two columns — the title +
   * description in a fixed left column, the controls in a flexible right column
   * — the "settings page" pattern. Collapses to stacked rows on narrow widths.
   */
  horizontal?: boolean;
}

/**
 * FormSection groups related form fields under a titled, optionally-described
 * header so long forms read as a handful of clear sections instead of one flat
 * wall of inputs. It is layout-only (no form state) so it can wrap any controls.
 */
export default function FormSection({
  title,
  description,
  icon,
  action,
  children,
  variant = "card",
  horizontal = false,
}: FormSectionProps) {
  const heading = (
    <Box direction="column" gap={1} style={{ minWidth: 0 }}>
      <Box direction="row" align="center" gap={2}>
        {icon}
        <Text as="h2" size="lg" weight="semibold">
          {title}
        </Text>
      </Box>
      {description && (
        <Text size="sm" color="muted">
          {description}
        </Text>
      )}
    </Box>
  );

  // Two-column "settings" layout: heading on the left, controls on the right.
  // The columns wrap (left above right) once the row can't hold ~640px.
  if (variant === "plain" && horizontal) {
    return (
      <Box direction="row" gap={6} align="start" wrap>
        <Box
          direction="column"
          gap={2}
          style={{ flex: "1 1 200px", maxWidth: 260, minWidth: 0 }}
        >
          {heading}
          {action}
        </Box>
        <Box
          direction="column"
          gap={3}
          style={{ flex: "3 1 420px", minWidth: 0 }}
        >
          {children}
        </Box>
      </Box>
    );
  }

  const header = (
    <Box direction="row" justify="between" align="start" gap={2}>
      {heading}
      {action}
    </Box>
  );

  if (variant === "plain") {
    return (
      <Box direction="column" gap={3}>
        {header}
        {children}
      </Box>
    );
  }

  return (
    <section className="form-section">
        <Box direction="column" gap={4}>
          {header}
          {children}
        </Box>
    </section>
  );
}
