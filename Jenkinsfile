pipeline {
    agent any

    stages {
        stage('Checkout') {
            steps {
                echo '🔄 Клонируем репозиторий...'
                // Клонируем репозиторий с GitHub
                git url: 'https://github.com/B216-lab/transport_web.git',
                    branch: 'main'
                
                echo '✅ Репозиторий успешно склонирован!'
            }
        }
        
        stage('Verify Files') {
            steps {
                echo '📋 Проверяем наличие файлов...'
                // Показываем структуру проекта
                sh 'ls -la'
                sh 'ls -la backend/ || echo "Папка backend не найдена"'
                sh 'ls -la frontend/ || echo "Папка frontend не найдена"'
                sh 'ls -la docker-compose.yml || echo "docker-compose.yml не найден"'
                sh 'ls -la Jenkinsfile && echo "Jenkinsfile найден ✅"'
            }
        }
    }

    post {
        success {
            echo 'Пайплайн успешно выполнен! Репозиторий готов.'
        }
        failure {
            echo ' Ошибка в пайплайне. Проверьте логи.'
        }
    }
}
