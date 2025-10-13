import Link from '@/components/ui/Link';

// src/app/not-found.tsx
export default function NotFound() {
  return (
    <main className="bg-white">
      <section className="py-20 px-6 text-center">
        <h1 className="text-3xl font-bold text-gray-900 mb-4">Page Not Found</h1>
        <p className="text-gray-600 mb-6">
          Sorry, the page you&apos;re looking for doesn&apos;t exist.
        </p>
        <Link href="/" className="text-[#2563EB] hover:underline">
          Return to Home
        </Link>
      </section>
    </main>
  );
}
