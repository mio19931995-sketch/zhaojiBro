import type { ReactNode } from 'react';
export function Button({ children, onClick, primary = false, disabled = false, title, className = '' }: { children: ReactNode; onClick?: () => void; primary?: boolean; disabled?: boolean; title?: string; className?: string }) {
  return <button className={`button ${primary ? 'primary' : ''} ${className}`} onClick={onClick} disabled={disabled} title={title}>{children}</button>;
}
export function Empty({ icon, title, text }: { icon: ReactNode; title: string; text: string }) {
  return <div className="empty"><span>{icon}</span><strong>{title}</strong><p>{text}</p></div>;
}
