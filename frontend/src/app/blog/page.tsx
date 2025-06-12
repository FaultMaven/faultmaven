import Link from 'next/link';
import { BookOpen, Lightbulb, RefreshCw, Users } from 'lucide-react';
import Button from '@/components/ui/Button';

export default function BlogPage() {
  return (
    <main className="min-h-screen bg-white dark:bg-slate-900">
      {/* Page Headline & Sub-headline */}
      <section className="py-20 text-center bg-white dark:bg-slate-900">
        <div className="max-w-4xl mx-auto px-6">
          <h1 className="text-4xl md:text-5xl font-bold text-slate-900 dark:text-slate-50 mb-6">
            The FaultMaven Blog: Insights on AIOps & Troubleshooting
          </h1>
          <h2 className="text-2xl font-semibold text-blue-700 dark:text-blue-400 mb-8">
            We&apos;re Preparing Your New Resource for Operational Excellence
          </h2>
          <p className="text-lg text-slate-700 dark:text-slate-300 max-w-2xl mx-auto mb-0">
            We&apos;re excited to be launching the official FaultMaven Blog shortly. This space will be dedicated to sharing deep insights, practical guides, and forward-thinking perspectives on the intersection of AI and modern operations. Our goal is to create a valuable resource for the SRE, DevOps, and broader engineering community.
          </p>
        </div>
      </section>

      {/* What to Expect Section */}
      <section className="py-16 bg-slate-50 dark:bg-slate-800/50">
        <div className="max-w-4xl mx-auto px-6">
          <h3 className="text-3xl font-bold text-slate-900 dark:text-slate-100 mb-6 text-center">What to Expect</h3>
          <p className="text-lg text-slate-700 dark:text-slate-300 mb-8 text-center">Here&apos;s a taste of what we&apos;ll be exploring:</p>
          <ul className="space-y-8 max-w-2xl mx-auto">
            <li className="flex items-start gap-5">
              <BookOpen className="w-8 h-8 text-blue-600 mt-1 flex-shrink-0" />
              <div>
                <span className="font-bold text-slate-800 dark:text-slate-200">Deep Dives into AIOps:</span> <span className="text-slate-700 dark:text-slate-300">The latest trends, challenges, and opportunities in AI for IT Operations.</span>
              </div>
            </li>
            <li className="flex items-start gap-5">
              <Lightbulb className="w-8 h-8 text-blue-600 mt-1 flex-shrink-0" />
              <div>
                <span className="font-bold text-slate-800 dark:text-slate-200">Troubleshooting Best Practices:</span> <span className="text-slate-700 dark:text-slate-300">Practical tips, advanced techniques, and strategies from seasoned engineers.</span>
              </div>
            </li>
            <li className="flex items-start gap-5">
              <RefreshCw className="w-8 h-8 text-blue-600 mt-1 flex-shrink-0" />
              <div>
                <span className="font-bold text-slate-800 dark:text-slate-200">FaultMaven Product Updates:</span> <span className="text-slate-700 dark:text-slate-300">A transparent look into our development journey, new feature highlights, and roadmap milestones.</span>
              </div>
            </li>
            <li className="flex items-start gap-5">
              <Users className="w-8 h-8 text-blue-600 mt-1 flex-shrink-0" />
              <div>
                <span className="font-bold text-slate-800 dark:text-slate-200">Thoughts from Our Team:</span> <span className="text-slate-700 dark:text-slate-300">Perspectives from the engineers and experts who are building FaultMaven.</span>
              </div>
            </li>
          </ul>
        </div>
      </section>

      {/* CTA Section */}
      <section className="py-16 bg-white dark:bg-slate-900">
        <div className="max-w-4xl mx-auto px-6 text-center">
          <h3 className="text-3xl font-bold text-slate-900 dark:text-slate-50 mb-6">Be the First to Read Our Insights</h3>
          <p className="text-lg text-slate-700 dark:text-slate-300 mb-8 max-w-2xl mx-auto">
            Want to be notified when our first posts go live? Join our waitlist to receive all major FaultMaven announcements, including the launch of our blog and early access opportunities.
          </p>
          <Button asChild href="/waitlist" variant="primary">
            Join the Waitlist & Get Updates
          </Button>
        </div>
      </section>

      {/* Alternative Navigation */}
      <section className="py-12 bg-slate-50 dark:bg-slate-800/50">
        <div className="max-w-4xl mx-auto px-6 text-center text-lg text-slate-700 dark:text-slate-300">
          In the meantime, feel free to explore{' '}
          <Link href="/roadmap">Our Vision</Link>{' '}or check out our{' '}
          <Link href="/resources/faq">FAQ Page</Link>.
        </div>
      </section>
    </main>
  );
}
