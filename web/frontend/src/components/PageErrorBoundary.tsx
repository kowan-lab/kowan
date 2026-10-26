import { Component, type ErrorInfo, type ReactNode } from "react";
import { Button, Icon } from "@/ui/kit";
import EmptyState from "./EmptyState";

interface Props {
  children: ReactNode;
}

interface State {
  error: Error | null;
}

/**
 * Catches a render error in the page content so the shell (sidebar, top bar)
 * stays usable: the page shows what failed and a retry instead of a blank
 * window. Mounted per route (keyed by path), so navigating away resets it.
 */
export default class PageErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error("page crashed", error, info.componentStack);
  }

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;
    return (
      <EmptyState
        icon={<Icon name="error-outline-rounded" size="lg" />}
        title="Something went wrong on this page"
        description={error.message || "An unexpected error stopped this page from rendering."}
        action={
          <Button variant="outline" color="neutral" iconLeft="refresh-rounded" onClick={() => this.setState({ error: null })}>
            Try again
          </Button>
        }
      />
    );
  }
}
