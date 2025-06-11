'use client'

import Button from '@/components/ui/Button';

export default function Error({ error, reset }: {
  error: Error
  reset: () => void
}) {
  return (
    <div className="grid min-h-screen place-items-center">
      <div className="text-center">
        <h1 className="text-2xl font-bold text-red-600">Something went wrong!</h1>
        <p className="mt-2 text-gray-600">{error.message}</p>
        <Button onClick={() => reset()} variant="primary" className="mt-4">
          Try Again
        </Button>
      </div>
    </div>
  )
}
