import { Handshake, Key, Megaphone } from 'lucide-react';

const benefits = [
  {
    title: 'Early Access',
    description: 'Be among the first to experience our platform and shape its future development.',
    icon: Key,
    accent: 'bg-blue-500',
  },
  {
    title: 'Exclusive Updates',
    description: 'Receive insider information about our progress, features, and launch plans.',
    icon: Megaphone,
    accent: 'bg-green-500',
  },
  {
    title: 'Priority Support',
    description: 'Get dedicated support and direct communication with our team.',
    icon: Handshake,
    accent: 'bg-purple-500',
  },
];

export default function Benefits() {
  return (
    <section className="py-16 bg-slate-50 dark:bg-slate-800/50" aria-labelledby="benefits-heading">
      <div className="max-w-4xl mx-auto px-6">
        <h2 id="benefits-heading" className="text-center font-semibold text-slate-800 dark:text-slate-200 mb-10 text-2xl md:text-3xl">
          By joining our waitlist, you&apos;ll get:
        </h2>
        <div className="grid md:grid-cols-3 gap-8">
          {benefits.map((benefit, index) => (
            <div
              key={index}
              className="relative bg-white dark:bg-slate-900 p-8 rounded-2xl shadow-md border border-slate-200 dark:border-slate-700 transition hover:shadow-lg group"
            >
              <div className={`absolute -top-4 left-6 w-10 h-2 rounded-full ${benefit.accent} opacity-80 group-hover:scale-105 transition`} />
              <div className="flex flex-col items-center">
                <div className="flex-shrink-0 w-12 h-12 flex items-center justify-center rounded-full bg-blue-50 dark:bg-blue-900/30 mb-4" aria-hidden="true">
                  <benefit.icon className="w-7 h-7 text-blue-600 dark:text-blue-400" />
                </div>
                <h3 className="font-bold text-lg text-slate-800 dark:text-slate-200 mb-2 text-center">
                  {benefit.title}
                </h3>
                <p className="text-base text-slate-600 dark:text-slate-400 text-center">
                  {benefit.description}
                </p>
              </div>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
} 