pipeline {
    agent any

    stages {
        stage('Deploy') {
            steps {
                sh '''
                    set -e
                    cd /opt/proj3/transport-web

                    git fetch origin main
                    git checkout server-local

                    if git merge origin/main --no-edit 2>/dev/null; then
                        :
                    else
                        git reset --hard origin/main
                    fi
                    
                    git status

                    docker compose down
                    docker compose up -d --build
                    docker compose ps
                '''
            }
        }
    }
}
