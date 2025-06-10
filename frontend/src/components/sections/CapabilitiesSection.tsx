'use client';

import {
  IconChartBar,
  IconDocumentText,
  IconSparkles,
} from '@/components/icons/homepage';

export default function CapabilitiesSection() {
  return (
    <section className="py-24 bg-white dark:bg-slate-900">
      <div className="max-w-6xl mx-auto px-6">
        <h2 className="text-3xl font-bold text-center text-slate-900 dark:text-slate-50 mb-8">
          FaultMaven 1.0 In Action: What Key Features Can I Leverage?
        </h2>
        <p className="text-lg text-slate-600 dark:text-slate-400 mb-12 max-w-3xl mx-auto text-center">
          FaultMaven 1.0 puts powerful AI assistance directly at your fingertips, integrated seamlessly via its browser extension. Here are the key capabilities you can use right now to transform your troubleshooting:
        </p>
        <div className="grid grid-cols-1 md:grid-cols-3 gap-8 text-left">
          <div className="p-8 border border-slate-200 dark:border-slate-800 rounded-lg shadow-sm bg-slate-50 dark:bg-slate-800/50">
            <IconChartBar className="w-8 h-8 text-blue-600 mb-4" />
            <h3 className="text-2xl font-semibold text-slate-900 dark:text-slate-100 mb-2">
              AI-Powered Diagnostics & Root Cause Insights
            </h3>
            <p className="text-slate-600 dark:text-slate-400">
              Instantly gain clarity on complex issues. Provide FaultMaven with logs, error messages, or system context, and its AI will analyze the data to identify critical patterns, suggest likely root causes, and guide you with actionable next steps. <em className="text-slate-500 dark:text-slate-400/80 block mt-2">This means less time guessing and more time resolving, significantly cutting down your MTTR.</em>
            </p>
          </div>
          <div className="p-8 border border-slate-200 dark:border-slate-800 rounded-lg shadow-sm bg-slate-50 dark:bg-slate-800/50">
            <IconDocumentText className="w-8 h-8 text-blue-600 mb-4" />
            <h3 className="text-2xl font-semibold text-slate-900 dark:text-slate-100 mb-2">
              Instant Summaries & Draft Documentation
            </h3>
            <p className="text-slate-600 dark:text-slate-400">
              Effortlessly capture crucial information. As you troubleshoot, FaultMaven can generate concise summaries of your findings and help draft initial notes for documentation or post-mortem reports. <em className="text-slate-500 dark:text-slate-400/80 block mt-2">This saves you from tedious manual note-taking and ensures valuable knowledge is preserved and easily shared.</em>
            </p>
          </div>
          <div className="p-8 border border-slate-200 dark:border-slate-800 rounded-lg shadow-sm bg-slate-50 dark:bg-slate-800/50">
            <IconSparkles className="w-8 h-8 text-blue-600 mb-4" />
            <h3 className="text-2xl font-semibold text-slate-900 dark:text-slate-100 mb-2">
              Intelligent Personal Knowledge Reuse
            </h3>
            <p className="text-slate-600 dark:text-slate-400">
              Stop reinventing the wheel with every new alert. Feed FaultMaven your existing runbooks, proven solutions from past incidents, and personal troubleshooting tips. It then intelligently surfaces this relevant, curated knowledge when similar situations arise. <em className="text-slate-500 dark:text-slate-400/80 block mt-2">This empowers you with consistent, effective fixes drawn from collective expertise, reducing repetitive research and speeding up resolutions.</em>
            </p>
          </div>
        </div>
      </div>
    </section>
  );
}
