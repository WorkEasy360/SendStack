export default function Skeleton({ className = "h-4 w-full" }) {
    return <div className={`skeleton rounded-md ${className}`} aria-hidden="true" />;
}

/** Placeholder grid matching the StatCard layout. */
export function StatCardSkeleton({ count = 4 }) {
    return (
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
            {Array.from({ length: count }).map((_, index) => (
                <div key={index} className="bg-white border border-ink-200 rounded-card shadow-card p-5">
                    <Skeleton className="h-3 w-20 mb-3" />
                    <Skeleton className="h-7 w-14" />
                </div>
            ))}
        </div>
    );
}

/** Placeholder rows matching the DataTable layout. */
export function TableSkeleton({ rows = 5, columns = 4 }) {
    return (
        <div className="bg-white border border-ink-200 rounded-card shadow-card overflow-hidden">
            <div className="bg-ink-50 border-b border-ink-200 px-4 py-3 flex gap-4">
                {Array.from({ length: columns }).map((_, index) => (
                    <Skeleton key={index} className="h-3 flex-1" />
                ))}
            </div>
            {Array.from({ length: rows }).map((_, rowIndex) => (
                <div key={rowIndex} className="px-4 py-4 flex gap-4 border-b border-ink-100 last:border-0">
                    {Array.from({ length: columns }).map((_, colIndex) => (
                        <Skeleton key={colIndex} className="h-3.5 flex-1" />
                    ))}
                </div>
            ))}
        </div>
    );
}

/** Placeholder cards for grid layouts (templates, accounts). */
export function CardGridSkeleton({ count = 4, className = "grid md:grid-cols-2 gap-4" }) {
    return (
        <div className={className}>
            {Array.from({ length: count }).map((_, index) => (
                <div key={index} className="bg-white border border-ink-200 rounded-card shadow-card p-6">
                    <Skeleton className="h-4 w-32 mb-3" />
                    <Skeleton className="h-3 w-full mb-2" />
                    <Skeleton className="h-3 w-3/4 mb-5" />
                    <Skeleton className="h-8 w-24" />
                </div>
            ))}
        </div>
    );
}
