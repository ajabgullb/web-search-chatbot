import { Link } from 'react-router'

export function NotFoundPage() {
  return (
    <main className="mx-auto flex min-h-dvh max-w-lg flex-col justify-center gap-4 px-6 text-center">
      <h1 className="text-3xl font-semibold">Page not found</h1>
      <p className="text-muted-foreground">
        That page does not exist, or you do not have access to it.
      </p>
      <p>
        <Link to="/" className="underline underline-offset-4">
          Back to the home page
        </Link>
      </p>
    </main>
  )
}
