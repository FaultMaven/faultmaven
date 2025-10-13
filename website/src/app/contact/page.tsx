import Link from '@/components/ui/Link';
import { Mail, Handshake, TrendingUp, Users, Info, HeartHandshake } from 'lucide-react';

const subjectLines = [
  {
    icon: <Handshake className="w-6 h-6 text-blue-600 dark:text-blue-400" />, 
    title: 'Design Partner Inquiry / Early Access Feedback',
    desc: 'For experienced SREs & Ops Engineers interested in our Private Preview, providing feedback on FaultMaven 1.0, or sharing troubleshooting challenges.'
  },
  {
    icon: <TrendingUp className="w-6 h-6 text-blue-600 dark:text-blue-400" />, 
    title: 'Investor Inquiry',
    desc: 'For discussions regarding strategic investment opportunities or to request our Vision Deck.'
  },
  {
    icon: <Users className="w-6 h-6 text-blue-600 dark:text-blue-400" />, 
    title: 'Talent & Collaboration Inquiry',
    desc: 'If you&apos;re passionate about our mission and interested in exploring future roles or contributing your expertise.'
  },
  {
    icon: <Info className="w-6 h-6 text-blue-600 dark:text-blue-400" />, 
    title: 'General Question / Other',
    desc: 'For any other questions, comments, or information requests about FaultMaven.'
  },
];

const navCards = [
  {
    icon: <TrendingUp className="w-8 h-8 text-blue-600 dark:text-blue-400 mx-auto" />, 
    title: 'Explore Our Vision',
    desc: 'See our long-term plans and product evolution.',
    href: '/roadmap',
  },
  {
    icon: <Users className="w-8 h-8 text-blue-600 dark:text-blue-400 mx-auto" />, 
    title: 'Join the Waitlist',
    desc: 'Get key updates on progress and announcements.',
    href: '/waitlist',
  },
  {
    icon: <Info className="w-8 h-8 text-blue-600 dark:text-blue-400 mx-auto" />, 
    title: 'Read the FAQ',
    desc: 'Find answers to common questions.',
    href: '/faq',
  },
];

export default function ContactPage() {
  return (
    <main className="min-h-screen bg-gradient-to-b from-blue-50/60 via-white to-slate-100 dark:from-slate-900 dark:via-slate-900 dark:to-slate-800">
      {/* Hero Section */}
      <section className="py-20 flex items-center justify-center">
        <div className="max-w-3xl w-full px-6">
          <div className="rounded-2xl shadow-xl bg-white/90 dark:bg-slate-900/90 p-10 border border-slate-200 dark:border-slate-800 text-center">
            <h1 className="text-4xl md:text-5xl font-bold text-slate-900 dark:text-slate-50 mb-6">
              Connect with FaultMaven
            </h1>
            <p className="text-lg text-slate-700 dark:text-slate-300 max-w-2xl mx-auto">
              We&apos;re building FaultMaven to solve real-world operational challenges, and we believe the best way to do that is by engaging with a vibrant community of engineers, innovators, partners, and forward-thinkers. Whether you have a specific question, wish to share your expertise, explore a collaboration, or simply learn more, we&apos;re ready to listen.<br />
              Your insights, feedback, and inquiries are invaluable as we progress from our initial FaultMaven 1.0 offering towards our broader vision.
            </p>
          </div>
        </div>
      </section>

      {/* Contact Card Section */}
      <section className="py-16 px-6">
        <div className="max-w-4xl mx-auto grid md:grid-cols-2 gap-10 items-stretch">
          {/* Left: Email */}
          <div className="flex flex-col justify-center items-center bg-blue-50 dark:bg-blue-900/30 rounded-2xl shadow-md border border-blue-100 dark:border-blue-900 p-10 text-center">
            <Mail className="w-10 h-10 text-blue-600 dark:text-blue-400 mb-4" />
            <a href="mailto:support@faultmaven.ai" className="text-2xl font-bold text-blue-700 dark:text-blue-400 hover:underline mb-2">
              support@faultmaven.ai
            </a>
            <p className="text-lg text-slate-700 dark:text-slate-300 mb-0">We&apos;d love to hear from you!</p>
          </div>
          {/* Right: Subject Lines */}
          <div className="flex flex-col justify-center bg-white dark:bg-slate-900 rounded-2xl shadow-md border border-slate-200 dark:border-slate-700 p-8">
            <h2 className="text-2xl font-bold text-slate-900 dark:text-slate-100 mb-4 text-center">How to Reach Us</h2>
            <p className="text-slate-700 dark:text-slate-300 text-center mb-4 text-base">
              When emailing us, please use one of the following subject lines to help us respond quickly:
            </p>
            <ul className="space-y-4">
              {subjectLines.map((item, idx) => (
                <li key={idx} className="flex items-start gap-4">
                  <span className="mt-1">{item.icon}</span>
                  <div>
                    <span className="font-semibold text-slate-800 dark:text-slate-200">{item.title}</span>
                    <br />
                    <span className="text-slate-600 dark:text-slate-400 text-sm">{item.desc}</span>
                  </div>
                </li>
              ))}
            </ul>
          </div>
        </div>
      </section>

      {/* Other Ways to Engage Section */}
      <section className="py-16 bg-slate-50 dark:bg-slate-800/50">
        <div className="max-w-4xl mx-auto px-6">
          <div className="grid md:grid-cols-3 gap-8">
            <h2 className="col-span-full text-2xl md:text-3xl font-bold text-slate-900 dark:text-slate-100 mb-8 text-center">More Ways to Connect</h2>
            {navCards.map((card, idx) => (
              <Link key={idx} href={card.href}>
                <div>{card.icon}</div>
                <h3 className="text-xl font-bold text-slate-900 dark:text-slate-100 mt-4 mb-2 group-hover:text-blue-700 dark:group-hover:text-blue-400 transition">
                  {card.title}
                </h3>
                <p className="text-slate-600 dark:text-slate-400 text-base mb-0">{card.desc}</p>
              </Link>
            ))}
          </div>
        </div>
      </section>

      {/* Commitment Banner Section */}
      <section className="py-16 px-6">
        <div className="max-w-4xl mx-auto">
          <div className="rounded-2xl bg-blue-50 dark:bg-blue-900/30 shadow-md border border-blue-100 dark:border-blue-900 p-8 flex flex-col items-center text-center">
            <HeartHandshake className="w-10 h-10 text-blue-600 dark:text-blue-400 mb-4" />
            <h2 className="text-2xl md:text-3xl font-bold text-slate-900 dark:text-slate-100 mb-2">
              Our Commitment
            </h2>
            <p className="text-lg text-slate-700 dark:text-slate-300 max-w-2xl mx-auto mb-2">
              We value your interest and aim to respond to all inquiries as promptly as possible. As we are a focused team in a dynamic development phase, please allow for a reasonable response time.
            </p>
            <p className="text-lg text-slate-700 dark:text-slate-300 max-w-2xl mx-auto">
              We look forward to connecting with you!
            </p>
          </div>
        </div>
      </section>
    </main>
  );
}
