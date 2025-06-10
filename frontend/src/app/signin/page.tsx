import SignInForm from '@/components/auth/SignInForm';
import Link from 'next/link';
import Image from 'next/image';

export default function SignInPage() {
  return (
    <main className="flex flex-col items-center justify-center min-h-screen bg-slate-50 dark:bg-slate-900 p-6">
      <div className="mb-8">
        <Link href="/">
            <Image
              src="/images/fmlogo-light.svg"
              alt="FaultMaven Logo"
              className="h-10 w-auto dark:hidden"
              width={40}  // Add appropriate width
              height={10}  // Add appropriate height
            />
            <Image
              src="/images/fmlogo-dark.svg"
              alt="FaultMaven Logo"
              className="h-10 w-auto hidden dark:block"
              width={40}  // Add appropriate width
              height={10}  // Add appropriate height
            />
        </Link>
      </div>
      <SignInForm />
    </main>
  );
}
