'use client';

export default function GettingStartedSection() {
  return (
    <section className="py-32 bg-slate-50 dark:bg-slate-900">
      <div className="max-w-6xl mx-auto px-6">
        <h2 className="text-4xl font-bold text-center text-slate-900 dark:text-slate-50 mb-6">
          Getting Started with FaultMaven 1.0: How Do I Use It?
        </h2>
        <p className="text-lg text-slate-600 dark:text-slate-400 mb-16 max-w-4xl mx-auto text-center">
          FaultMaven 1.0 is your personal AI Copilot for troubleshooting. It&apos;s designed for engineers who want to solve operational problems faster—without the friction of complex setup or vendor lock-in. Here&apos;s how to get started:
        </p>

        <div className="grid grid-cols-1 md:grid-cols-3 gap-10">
          {/* Step 1 */}
          <div className="flex">
            <div className="flex-shrink-0 w-12 h-12 bg-blue-600 text-white rounded-full flex items-center justify-center font-bold text-2xl mr-6">1</div>
            <div>
              <h3 className="text-2xl font-semibold text-slate-900 dark:text-slate-100 mb-4">Instant Install: Get Ready in Seconds</h3>
              <p className="text-lg text-slate-600 dark:text-slate-400">Add the FaultMaven browser extension with just a few clicks. It&apos;s an effortless setup, allowing you to start validating its capabilities immediately without any upfront commitment or system changes.</p>
            </div>
          </div>
          {/* Step 2 */}
          <div className="flex">
            <div className="flex-shrink-0 w-12 h-12 bg-blue-600 text-white rounded-full flex items-center justify-center font-bold text-2xl mr-6">2</div>
            <div>
              <h3 className="text-2xl font-semibold text-slate-900 dark:text-slate-100 mb-4">Share Context: Your Data, Your Way</h3>
              <p className="text-lg text-slate-600 dark:text-slate-400">When you need assistance, engage FaultMaven via its intuitive side-panel. Share error messages, logs (by copy-pasting or uploading files), or the context of your current browser page. You can also ask general questions or submit data snippets for AI analysis. You control the interaction.</p>
            </div>
          </div>
          {/* Step 3 */}
          <div className="flex">
            <div className="flex-shrink-0 w-12 h-12 bg-blue-600 text-white rounded-full flex items-center justify-center font-bold text-2xl mr-6">3</div>
            <div>
              <h3 className="text-2xl font-semibold text-slate-900 dark:text-slate-100 mb-4">Get Actionable Insights: Iterate and Resolve</h3>
              <p className="text-lg text-slate-600 dark:text-slate-400">FaultMaven&apos;s AI instantly analyzes your input, initiating a continuous and progressive dialogue. Receive actionable insights, potential root cause hypotheses, clear answers, and intelligent next-step recommendations that evolve as you explore the problem together, guiding you towards faster resolution.</p>
            </div>
          </div>
        </div>

        <p className="text-center text-xl font-semibold text-slate-700 dark:text-slate-300 mt-20">
          No vendor lock-in. No integration hell. Just smarter troubleshooting, right away.
        </p>
      </div>
    </section>
  );
}
