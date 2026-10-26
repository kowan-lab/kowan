import { useLocation, useNavigate } from "react-router";
import { Button } from "@/ui/kit";
import EmptyState from "../components/EmptyState";

/** Any address the app does not know: say so, and offer the way back. */
export default function NotFound() {
  const navigate = useNavigate();
  const { pathname } = useLocation();
  return (
    <EmptyState
      title="Page not found"
      description={<>There is no page at <code className="font-mono">{pathname}</code>. It may have moved, or the link is mistyped.</>}
      action={<Button onClick={() => navigate("/")}>Go to the overview</Button>}
    />
  );
}
