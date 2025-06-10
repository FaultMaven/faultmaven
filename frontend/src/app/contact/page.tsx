import { Handshake, Info, Mail, TrendingUp, Users } from 'lucide-react';
import Link from 'next/link';

const inquiryTypes = [
    {
        icon: Handshake,
        subject: 'Design Partner Inquiry / Early Access Feedback',
        description: 'For experienced SREs & Ops Engineers interested in our Private Preview, providing feedback on FaultMaven 1.0, or sharing troubleshooting challenges.'
    },
    {
        icon: TrendingUp,
        subject: 'Investor Inquiry',
        description: 'For discussions regarding strategic investment opportunities or to request our Vision Deck.'
    },
    {
        icon: Users,
        subject: 'Talent & Collaboration Inquiry',
        description: "If you're passionate about our mission and interested in exploring future roles or contributing your expertise."
    },
    {
        icon: Info,
        subject: 'General Question / Other',
        description: 'For all other inquiries or comments about FaultMaven.'
    }
];

export default function ContactPage() {
    return (
        <main>
            <section className="py-20 text-center bg-white dark:bg-slate-900">
                <div className="max-w-4xl mx-auto px-6">
                    <h1 className="text-4xl md:text-5xl font-bold text-slate-900 dark:text-slate-50">
                        Connect with FaultMaven
                    </h1>
                    <h2 className="mt-6 text-3xl font-semibold text-slate-700 dark:text-slate-300">
                        Let&apos;s Start a Conversation
                    </h2>
                    <div className="mt-6 text-lg text-slate-600 dark:text-slate-400 max-w-3xl mx-auto space-y-4">
                        <p>
                            We&apos;re building FaultMaven to solve real-world operational challenges, and we believe the best way to do that is by engaging with a vibrant community of engineers, innovators, partners, and forward-thinkers. Whether you have a specific question, wish to share your expertise, explore a collaboration, or simply learn more, we&apos;re ready to listen.
                        </p>
                        <p>
                            Your insights, feedback, and inquiries are invaluable as we progress from our initial FaultMaven 1.0 offering towards our broader vision.
                        </p>
                    </div>
                </div>
            </section>

            <section className="py-16 bg-slate-50 dark:bg-slate-800/50">
                <div className="max-w-4xl mx-auto px-6">
                    <div className="text-center mb-16">
                        <h2 className="text-3xl font-bold text-slate-800 dark:text-slate-200 mb-4">
                            How to Reach Us
                        </h2>
                        <p className="text-lg text-slate-600 dark:text-slate-400 max-w-2xl mx-auto">
                            For all inquiries, the most direct way to connect is by emailing our team. To help us understand your specific interest, please consider using one of the subject lines below.
                        </p>
                    </div>

                    <div className="max-w-md mx-auto bg-white dark:bg-slate-900 rounded-2xl shadow-lg border border-slate-200 dark:border-slate-700 p-8 mb-16 text-center">
                        <h3 className="text-2xl font-semibold text-slate-700 dark:text-slate-300">
                            Your Direct Line to Our Team
                        </h3>
                        <a
                            href="mailto:support@faultmaven.ai"
                            className="mt-4 inline-flex items-center justify-center gap-2 text-xl font-bold text-white bg-blue-600 hover:bg-blue-700 transition rounded-lg px-8 py-4 w-full"
                        >
                            <Mail className="w-6 h-6" />
                            support@faultmaven.ai
                        </a>
                    </div>

                    <div className="grid md:grid-cols-2 gap-6">
                        {inquiryTypes.map((inquiry, index) => (
                            <div
                                key={index}
                                className="bg-white dark:bg-slate-900 p-6 rounded-xl border border-slate-200 dark:border-slate-700 shadow-sm transition-shadow hover:shadow-md"
                            >
                                <div className="flex items-start gap-4">
                                    <div className="flex-shrink-0 w-10 h-10 bg-blue-100 dark:bg-blue-900/50 rounded-full flex items-center justify-center">
                                        <inquiry.icon
                                            className="w-5 h-5 text-blue-600 dark:text-blue-400"
                                        />
                                    </div>
                                    <div>
                                        <h3 className="text-2xl font-semibold text-slate-800 dark:text-slate-200">
                                            {inquiry.subject}
                                        </h3>
                                        <p className="text-sm text-slate-500 dark:text-slate-400 mt-1">
                                            {inquiry.description}
                                        </p>
                                    </div>
                                </div>
                            </div>
                        ))}
                    </div>
                </div>
            </section>

            <section className="py-20 bg-white dark:bg-slate-900">
                <div className="max-w-4xl mx-auto px-6 text-center">
                    <h2 className="text-3xl font-bold text-slate-800 dark:text-slate-200 mb-10">
                        Other Ways to Engage
                    </h2>
                    <div className="grid md:grid-cols-3 gap-8">
                        <div className="p-6 rounded-lg">
                            <h3 className="text-2xl font-semibold text-lg text-slate-800 dark:text-slate-200">
                                Explore Our Vision
                            </h3>
                            <p className="text-slate-600 dark:text-slate-400 mt-2 mb-4">
                                See our long-term plans and product evolution.
                            </p>
                            <Link
                                href="/roadmap"
                                className="font-medium text-blue-600 dark:text-blue-400 hover:underline"
                            >
                                View Roadmap &rarr;
                            </Link>
                        </div>
                        <div className="p-6 rounded-lg">
                            <h3 className="text-2xl font-semibold text-lg text-slate-800 dark:text-slate-200">
                                Join the Waitlist
                            </h3>
                            <p className="text-slate-600 dark:text-slate-400 mt-2 mb-4">
                                Get key updates on progress and announcements.
                            </p>
                            <Link
                                href="/waitlist"
                                className="font-medium text-blue-600 dark:text-blue-400 hover:underline"
                            >
                                Get Updates &rarr;
                            </Link>
                        </div>
                        <div className="p-6 rounded-lg">
                            <h3 className="text-2xl font-semibold text-lg text-slate-800 dark:text-slate-200">
                                Read the FAQ
                            </h3>
                            <p className="text-slate-600 dark:text-slate-400 mt-2 mb-4">
                                Find answers to common questions.
                            </p>
                            <Link
                                href="/faq"
                                className="font-medium text-blue-600 dark:text-blue-400 hover:underline"
                            >
                                Visit FAQ &rarr;
                            </Link>
                        </div>
                    </div>
                </div>
            </section>

            <section className="py-16 bg-slate-50 dark:bg-slate-800/50">
                <div className="max-w-3xl mx-auto px-6 text-center">
                    <h3 className="text-2xl font-semibold text-slate-700 dark:text-slate-300 mb-3">
                        Our Commitment
                    </h3>
                    <p className="text-slate-500 dark:text-slate-400 max-w-xl mx-auto">
                        We value your interest and aim to respond to all inquiries as promptly as possible. As we are a focused team in a dynamic development phase, please allow for a reasonable response time. We look forward to connecting with you!
                    </p>
                </div>
            </section>
        </main>
    );
}
