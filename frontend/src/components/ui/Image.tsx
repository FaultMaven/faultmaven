import NextImage, { ImageProps as NextImageProps } from 'next/image';
import { useState } from 'react';

interface ImageProps extends Omit<NextImageProps, 'onLoadingComplete'> {
  fallbackSrc?: string;
}

export default function Image({ 
  src, 
  alt, 
  fallbackSrc = '/images/placeholder.png',
  className = '',
  ...props 
}: ImageProps) {
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState(false);

  return (
    <div className={`relative overflow-hidden ${className}`}>
      <NextImage
        src={error ? fallbackSrc : src}
        alt={alt}
        className={`
          duration-700 ease-in-out
          ${isLoading ? 'scale-110 blur-2xl grayscale' : 'scale-100 blur-0 grayscale-0'}
        `}
        onLoadingComplete={() => setIsLoading(false)}
        onError={() => setError(true)}
        {...props}
      />
    </div>
  );
} 