import { BrainCircuit, Rocket, Users, Wrench } from 'lucide-react';

export default function BlogPage() {
  return (
    <main>
      <section className="py-20 text-center bg-white dark:bg-slate-900">
        <div className="max-w-4xl mx-auto px-6">
          <h1 className="text-4xl md:text-5xl font-bold text-slate-900 dark:text-slate-50">
            FaultMaven Insights: Coming Soon
          </h1>
          <p className="mt-4 text-xl text-slate-600 dark:text-slate-400">
            We&apos;re Preparing Your New Resource for Operational Excellence
          </p>
        </div>
      </section>

      <div className="py-16 bg-white dark:bg-slate-900">
        <div className="max-w-4xl mx-auto px-6">
          <div className="prose prose-lg prose-slate dark:prose-invert mx-auto text-center">
            <p>
              We&apos;re excited to be launching the official FaultMaven Blog shortly. This space will be dedicated to sharing deep insights, practical guides, and forward-thinking perspectives on the intersection of AI and modern operations. Our goal is to create a valuable resource for the SRE, DevOps, and broader engineering community.
            </p>
          </div>

          <div className="mt-12">
            <h3 className="text-2xl font-semibold text-center text-slate-800 dark:text-slate-200 mb-8">Here&apos;s a taste of what we&apos;ll be exploring:</h3>
            <div className="grid md:grid-cols-2 gap-8 max-w-3xl mx-auto">
              <div className="flex items-start gap-4">
                <div className="flex-shrink-0 w-10 h-10 rounded-full bg-blue-100 dark:bg-blue-900/50 flex items-center justify-center">
                  <BrainCircuit className="w-5 h-5 text-blue-600 dark:text-blue-400" />
                </div>
                <div>
                  <h4 className="text-xl font-semibold text-slate-800 dark:text-slate-200">Deep Dives into AIOps</h4>
                  <p className="text-slate-600 dark:text-slate-400">The latest trends, challenges, and opportunities in AI for IT Operations.</p>
                </div>
              </div>
              <div className="flex items-start gap-4">
                <div className="flex-shrink-0 w-10 h-10 rounded-full bg-blue-100 dark:bg-blue-900/50 flex items-center justify-center">
                  <Wrench className="w-5 h-5 text-blue-600 dark:text-blue-400" />
                </div>
                <div>
                  <h4 className="text-xl font-semibold text-slate-800 dark:text-slate-200">Troubleshooting Best Practices</h4>
                  <p className="text-slate-600 dark:text-slate-400">Practical tips, advanced techniques, and strategies from seasoned engineers.</p>
                </div>
              </div>
              <div className="flex items-start gap-4">
                <div className="flex-shrink-0 w-10 h-10 rounded-full bg-blue-100 dark:bg-blue-900/50 flex items-center justify-center">
                  <Rocket className="w-5 h-5 text-blue-600 dark:text-blue-400" />
                </div>
                <div>
                  <h4 className="text-xl font-semibold text-slate-800 dark:text-slate-200">FaultMaven Product Updates</h4>
                  <p className="text-slate-600 dark:text-slate-400">A transparent look into our development journey, new feature highlights, and roadmap milestones.</p>
                </div>
              </div>
              <div className="flex items-start gap-4">
                <div className="flex-shrink-0 w-10 h-10 rounded-full bg-blue-100 dark:bg-blue-900/50 flex items-center justify-center">
                  <Users className="w-5 h-5 text-blue-600 dark:text-blue-400" />
                </div>
                <div>
                  <h4 className="text-xl font-semibold text-slate-800 dark:text-slate-200">Thoughts from Our Team</h4>
                  <p className="text-slate-600 dark:text-slate-400">Perspectives from the engineers and experts who are building FaultMaven.</p>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>

      <section className="bg-slate-50 dark:bg-slate-800/50">
        <div className="max-w-3xl mx-auto py-16 px-6 text-center">
          <h2 className="text-3xl font-bold text-slate-900 dark:text-slate-100 mb-4">
            Be the First to Read Our Insights
          </h2>
          <p className="text-lg text-slate-600 dark:text-slate-400 mb-8">
            Want to be notified when our first posts go live? Join our waitlist to receive all major FaultMaven announcements, including the launch of our blog and early access opportunities.
          </p>
          <div className="mb-8">
            <a 
              href="/waitlist"
              className="inline-flex items-center justify-center px-8 py-4 text-lg font-medium rounded-md shadow-sm text-white bg-blue-600 hover:bg-blue-700 transition duration-200"
            >
              Join the Waitlist & Get Updates
            </a>
          </div>
          <div className="prose prose-slate dark:prose-invert mx-auto">
            <p>
              In the meantime, feel free to explore <a href="/roadmap">Our Vision</a> or check out our <a href="/faq">FAQ Page</a>.
            </p>
          </div>
        </div>
      </section>
    </main>
  );
}
