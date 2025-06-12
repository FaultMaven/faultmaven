'use client';

import {
  IconBellSlash,
  IconDocumentMinus,
  IconLoop,
} from '@/components/icons/homepage';

export default function ProblemSection() {
  return (
    <section className="py-32 bg-white dark:bg-slate-900">
      <div className="max-w-6xl mx-auto px-6">
        <h2 className="text-4xl font-bold text-center text-slate-900 dark:text-slate-50 mb-6">
          Facing These Tough Operational Challenges?
        </h2>
        <p className="text-lg text-slate-600 dark:text-slate-400 mb-16 max-w-3xl mx-auto text-center">
          If you&apos;re an engineer on the front lines, you know the daily battle
          of keeping complex systems running smoothly. When issues strike, the
          pressure is immense, and often it feels like you&apos;re up against the
          same frustrating hurdles. You&apos;re not alone if you&apos;re grappling
          with:
        </p>
        <div className="grid grid-cols-1 md:grid-cols-3 gap-10">
          <div className="p-10 border border-slate-200 dark:border-slate-800 rounded-xl shadow-md bg-slate-50 dark:bg-slate-800/50 text-center hover:shadow-lg transition-shadow duration-200">
            <IconLoop className="w-12 h-12 text-blue-600 mx-auto mb-6" />
            <h3 className="text-2xl font-semibold text-slate-900 dark:text-slate-100 mb-4">
              Endless Toil & Slow Resolutions
            </h3>
            <p className="text-lg text-slate-600 dark:text-slate-400">
              Spending too much valuable time on repetitive, manual troubleshooting
              steps that lead to slow incident response, human error, and
              ever-increasing MTTR?
            </p>
          </div>
          <div className="p-10 border border-slate-200 dark:border-slate-800 rounded-xl shadow-md bg-slate-50 dark:bg-slate-800/50 text-center hover:shadow-lg transition-shadow duration-200">
            <IconBellSlash className="w-12 h-12 text-blue-600 mx-auto mb-6" />
            <h3 className="text-2xl font-semibold text-slate-900 dark:text-slate-100 mb-4">
              Crippling Alert Fatigue
            </h3>
            <p className="text-lg text-slate-600 dark:text-slate-400">
              Drowning in a sea of alerts, struggling to distinguish critical
              signals from noise, and worried about missing the truly urgent
              issues?
            </p>
          </div>
          <div className="p-10 border border-slate-200 dark:border-slate-800 rounded-xl shadow-md bg-slate-50 dark:bg-slate-800/50 text-center hover:shadow-lg transition-shadow duration-200">
            <IconDocumentMinus className="w-12 h-12 text-blue-600 mx-auto mb-6" />
            <h3 className="text-2xl font-semibold text-slate-900 dark:text-slate-100 mb-4">
              Vanishing Tribal Knowledge
            </h3>
            <p className="text-lg text-slate-600 dark:text-slate-400">
              Constantly reinventing the wheel due to scattered information,
              unclear procedures, or outdated runbooks whenever a critical problem
              arises?
            </p>
          </div>
        </div>
        <p className="text-center text-lg text-slate-600 dark:text-slate-400 mt-16">
          These obstacles don&apos;t just slow you down; they impact innovation,
          team morale, and the bottom line. FaultMaven is being built to change
          that.
        </p>
      </div>
    </section>
  );
}
