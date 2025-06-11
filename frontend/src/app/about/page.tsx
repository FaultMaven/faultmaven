import { Handshake, HeartHandshake, ShieldCheck, Target, Sparkles, Wrench, Zap } from 'lucide-react';
import Link from 'next/link';

export default function AboutPage() {
  return (
    <main>
      <section className="relative py-24 bg-slate-50 dark:bg-slate-800/50">
        <div className="absolute inset-0 bg-grid-slate-200/[0.05] dark:bg-grid-slate-700/[0.1] [mask-image:linear-gradient(to_bottom,white_5%,transparent_50%)]"></div>
        <div className="relative max-w-4xl mx-auto px-6 text-center">
          <h1 className="text-4xl md:text-5xl font-bold text-slate-900 dark:text-slate-50">
            Our Journey: Building Your Trusted AI Copilot for Operations
          </h1>
        </div>
      </section>

      <div className="py-20 bg-white dark:bg-slate-900">
        <div className="max-w-3xl mx-auto px-6 space-y-20">
          <section className="bg-slate-50/80 dark:bg-slate-800/60 rounded-2xl shadow-lg p-10 md:p-14 border border-slate-200 dark:border-slate-700">
            <h2 className="text-3xl md:text-4xl font-bold text-center text-slate-900 dark:text-slate-100 mb-10 md:mb-12">
              The Spark: Why FaultMaven Exists (From an Engineer&apos;s Perspective)
            </h2>
            <div className="flex flex-col md:flex-row items-start gap-10 md:gap-12">
              <div className="w-40 h-40 rounded-full bg-gradient-to-br from-blue-200 to-blue-400 dark:from-blue-900 dark:to-blue-600 flex items-center justify-center shadow-md mx-auto md:mx-0 border-4 border-white dark:border-slate-900">
                <div className="flex flex-col items-center space-y-2">
                  <Sparkles className="w-6 h-6 text-white" />
                  <Wrench className="w-6 h-6 text-white" />
                  <Zap className="w-6 h-6 text-white" />
                </div>
              </div>
              <div className="text-lg text-slate-700 dark:text-slate-300 space-y-4">
                <p>
                  FaultMaven wasn&apos;t born in a vacuum. It springs from{' '}
                  <span className="font-medium text-slate-800 dark:text-slate-200">
                    over a decade
                  </span>{' '}
                  spent on the front lines of complex operational environments,
                  navigating high-stakes incidents, and witnessing firsthand the
                  immense pressure on engineers to maintain system reliability
                  against ever-increasing complexity. Time and again, I saw
                  brilliant colleagues bogged down by the sheer volume of data,
                  the ambiguity of alerts, and the often-manual, painstaking
                  process of connecting disparate clues to find a root cause.
                </p>
                <p>
                  There was a persistent realization: while our monitoring
                  systems are essential, and automation handles routine tasks, a
                  significant gap remained when it came to the nuanced,
                  investigative work of deep troubleshooting. It became a core
                  belief that we could – and should – aspire to a more
                  intelligent partnership with our tools.{' '}
                  <strong className="font-medium text-slate-900 dark:text-white">
                    It was this conviction, born from real-world experience,
                    that ignited the journey to create FaultMaven.
                  </strong>
                </p>
              </div>
            </div>
          </section>

          <section className="bg-slate-50/80 dark:bg-slate-800/60 rounded-2xl shadow-lg p-10 md:p-14 border border-slate-200 dark:border-slate-700">
            <h2 className="text-3xl md:text-4xl font-bold text-center text-slate-900 dark:text-slate-100 mb-8 md:mb-10">
              Our Core Belief: The Next Level of Human-AI Partnership
            </h2>
            <div className="text-lg text-slate-700 dark:text-slate-300 space-y-6">
              <p>
                We see a future where Artificial Intelligence doesn&apos;t just
                automate tasks or provide surface-level answers, but acts as a
                profound amplifier of human expertise, especially in complex
                domains like operational troubleshooting. The creation of
                FaultMaven is driven by this core belief:{' '}
                <strong>
                  the next true leap in leveraging AI to solve critical
                  operational challenges hinges on a sophisticated synergy
                  between human insight and artificial intelligence.
                </strong>
              </p>
              <blockquote className="border-l-4 border-blue-500 pl-6 py-2 my-6 italic text-slate-600 dark:text-slate-400">
                Why not full automation for everything? Because for the most
                complex issues—those requiring deep diagnostic acumen,
                contextual understanding of business impact, and critical
                judgment—human expertise is irreplaceable. To truly harness AI&apos;s
                power effectively,{' '}
                <strong>
                  it is crucial that human experience identifies the problems,
                  defines the strategic questions, and guides the AI towards the
                  real targets.
                </strong>
              </blockquote>
              <p>
                This is why FaultMaven is conceived as an{' '}
                <strong>AI Copilot</strong>, not an autopilot. We embrace the
                analogy of &quot;starting with the machine&quot; for deep, rapid data
                analysis, pattern recognition, and hypothesis generation,
                reserving invaluable human expertise for strategic direction,
                critical decision-making, and final validation – much like how
                advanced medical diagnostics support a seasoned doctor. You, the
                engineer, will always be in command because you bear the
                ultimate responsibility. FaultMaven is designed to respect and
                amplify your authority by equipping you with unprecedented
                insight and analytical leverage.
              </p>
              <p>
                This philosophy of a human-centric, AI-augmented approach is the
                bedrock upon which FaultMaven is being built.
              </p>
            </div>
          </section>

          <section className="bg-slate-50/80 dark:bg-slate-800/60 rounded-2xl shadow-lg p-10 md:p-14 border border-slate-200 dark:border-slate-700">
            <h2 className="text-3xl md:text-4xl font-bold text-center text-slate-900 dark:text-slate-100 mb-8 md:mb-10">
              Our Core Mission: An AI Copilot You Can Trust and Rely On
            </h2>
            <div className="prose prose-lg prose-slate dark:prose-invert max-w-none">
              <p>
                Driven by the realization of AI&apos;s potential to profoundly assist
                human expertise,{' '}
                <strong>
                  our mission at FaultMaven is to embark on a journey,
                  leveraging the rapidly advancing power of AI, to enable
                  engineers like you to master increasingly complex operational
                  challenges.
                </strong>{' '}
                We are singularly focused on building an AI Copilot that you can
                genuinely{' '}
                <strong>trust and rely on, in good times and bad.</strong>
              </p>
              <p>
                For us, this means more than just smart algorithms. It means:
              </p>
            </div>
            <div className="mt-8 space-y-6">
              <div className="flex items-start gap-4 md:gap-6">
                <div className="flex-shrink-0 w-10 h-10 flex items-center justify-center rounded-full bg-blue-100 dark:bg-blue-900 shadow">
                  <Target className="w-6 h-6 text-blue-600 dark:text-blue-400" />
                </div>
                <div>
                  <h3 className="text-2xl font-semibold text-slate-800 dark:text-slate-200">
                    Delivering Actionable Clarity
                  </h3>
                  <p className="text-slate-600 dark:text-slate-400 mt-1">
                    Striving for insights that are not only intelligent but also
                    understandable and directly applicable to the problem at
                    hand.
                  </p>
                </div>
              </div>
              <div className="flex items-start gap-4 md:gap-6">
                <div className="flex-shrink-0 w-10 h-10 flex items-center justify-center rounded-full bg-blue-100 dark:bg-blue-900 shadow">
                  <ShieldCheck className="w-6 h-6 text-blue-600 dark:text-blue-400" />
                </div>
                <div>
                  <h3 className="text-2xl font-semibold text-slate-800 dark:text-slate-200">
                    Fostering Reliability
                  </h3>
                  <p className="text-slate-600 dark:text-slate-400 mt-1">
                    Committing to an AI that learns and improves consistently—with
                    your feedback and through transparent mechanisms—to become an
                    ever-more dependable partner.
                  </p>
                </div>
              </div>
              <div className="flex items-start gap-4 md:gap-6">
                <div className="flex-shrink-0 w-10 h-10 flex items-center justify-center rounded-full bg-blue-100 dark:bg-blue-900 shadow">
                  <HeartHandshake className="w-6 h-6 text-blue-600 dark:text-blue-400" />
                </div>
                <div>
                  <h3 className="text-2xl font-semibold text-slate-800 dark:text-slate-200">
                    Engineering with Empathy
                  </h3>
                  <p className="text-slate-600 dark:text-slate-400 mt-1">
                    Building FaultMaven with the passion of seasoned engineers who
                    have lived the pressures of operations and are dedicated to
                    creating a tool that truly alleviates pain and empowers its
                    users.
                  </p>
                </div>
              </div>
            </div>
            <div className="prose prose-lg prose-slate dark:prose-invert max-w-none mt-6">
              <p>
                Our mission is to provide an indispensable asset that helps you
                navigate the complexities of modern systems with greater
                confidence and efficiency.
              </p>
            </div>
          </section>
        </div>
      </div>

      <section className="py-20 bg-gradient-to-b from-slate-100/80 to-slate-50 dark:from-slate-900/80 dark:to-slate-800/60">
        <div className="max-w-3xl mx-auto px-6">
          <div className="rounded-2xl border border-blue-200 dark:border-blue-800 bg-white/80 dark:bg-slate-900/80 shadow-lg p-10 md:p-14 text-center">
            <h2 className="text-3xl md:text-4xl font-bold text-slate-900 dark:text-slate-100 mb-6">
              Building Together: An Invitation to Collaborate
            </h2>
            <p className="text-lg text-slate-600 dark:text-slate-400 mb-6">
              We believe the future of AIOps will be shaped by genuine
              collaboration between AI innovators and the brilliant engineers on
              the front lines. If FaultMaven&apos;s philosophy and mission resonate
              with you—if you&apos;re excited by the prospect of a new level of
              human-AI partnership in tackling operational complexity—we warmly
              invite you to connect and explore how we can build this future
              together.
            </p>
            <div className="prose prose-lg prose-slate dark:prose-invert max-w-none">
              <ul className="space-y-4 list-none !p-0">
                <li>
                  <Link href="/roadmap" className="text-blue-600 hover:text-blue-800 dark:text-blue-400 dark:hover:text-blue-300 underline transition-colors">
                    Learn more about our phased development and long-term goals on our Vision & Roadmap Page
                  </Link>.
                </li>
                <li>
                  To express interest in our early access programs, become a design partner, or discuss strategic opportunities, please{' '}
                  <Link href="/contact" className="text-blue-600 hover:text-blue-800 dark:text-blue-400 dark:hover:text-blue-300 underline transition-colors">
                    Contact Us
                  </Link>.
                </li>
                <li>
                  <Link href="/waitlist" className="font-semibold text-blue-600 hover:text-blue-800 dark:text-blue-400 dark:hover:text-blue-300 underline transition-colors">
                    Join Our Waitlist
                  </Link> to receive key updates on our progress, be notified of early access, and stay connected with the FaultMaven community.
                </li>
              </ul>
            </div>
          </div>
        </div>
      </section>
    </main>
  );
}
