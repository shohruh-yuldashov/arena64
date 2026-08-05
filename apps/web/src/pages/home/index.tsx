import { FormDemo } from "@/features/form-demo";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  Skeleton,
  Spinner,
} from "@/shared/ui";

/**
 * The shell's only route.
 *
 * Deliberately a **foundation exhibit** rather than a landing page: it
 * renders the loading primitives and the form wiring so that every piece
 * this phase built is mounted by the running app, not merely exported.
 * Anything that is only exported is something the next phase discovers is
 * broken.
 *
 * The lobby that eventually lives at `/` is A64-020.5's, and replacing
 * this file is that phase's first commit.
 */
export default function HomePage() {
  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-2xl font-semibold">Arena64</h1>
        <p className="text-muted-foreground text-sm">
          Application shell. No gameplay surface is built yet.
        </p>
      </div>

      <div className="grid gap-4 sm:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>Loading primitives</CardTitle>
            <CardDescription>
              A skeleton stands in for content whose shape is known; a spinner for work whose
              duration is not.
            </CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-3">
            <Skeleton className="h-4 w-3/4" />
            <Skeleton className="h-4 w-1/2" />
            <Spinner label="Example" />
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Form validation</CardTitle>
            <CardDescription>
              React Hook Form with a Zod resolver — one schema is both the rule and the type.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <FormDemo />
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
